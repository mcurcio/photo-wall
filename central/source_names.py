"""Logical operator Source names over immutable media query revisions.

A Photo source is saved from a draft (`save_draft`), as the next revision of its name with the
draft's query and pool (one pool per distinct query, so the pool carries over); a rename is a
draft saved under a new name. This service is the one home that writes a revision and its name:
a `media_sources` row is a revision, carrying its name and revision number, and the newest
revision of a name carries the name's head (`head`, `deleted`, `renamed_to`; decision 0034). A
name is one head (the unique index `media_sources_current_name`) and each of its revisions one
row (the unique `(name, revision)`), so two saves of one new name cannot both succeed.
"""

from __future__ import annotations

import re

from psycopg.types.json import Jsonb

from central.actor import Actor
from central.db import MEDIA_LOCK, Database
from central.infra.photo_library_store import PgPhotoLibrary
from central.media_repository import MediaRepository
from central.photo_library.model import DraftRef, PoolKey, SourceQuery
from central.photo_library.ports import LibraryRefused
from central.registry import RegistryError
from central.runtime_store import RuntimeStore

NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")


class SourceInUse(RegistryError):
    def __init__(self, scene_ids: tuple[str, ...]):
        super().__init__("source_in_use", 409)
        self.scene_ids = scene_ids


class SourceNameService:
    """Coordinate Source edits under Coordination, Runtime, then media locks."""

    def __init__(self, db: Database, repository: MediaRepository, runtime: RuntimeStore,
                 library: PgPhotoLibrary):
        self.db, self.repository, self.runtime, self.library = db, repository, runtime, library

    @staticmethod
    def _name(name: str) -> None:
        if NAME_PATTERN.fullmatch(name) is None:
            raise RegistryError("invalid_source_name", 422)

    @staticmethod
    def _versions_in(conn, name: str) -> tuple[dict | None, tuple[str, ...]]:
        """A name's head (its newest revision's row, locked: `source_ref`, `revision`,
        `deleted`, `renamed_to`) and every revision it has had, oldest first."""
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
        head = conn.execute("SELECT source_ref,revision,deleted,renamed_to FROM media_sources "
                            "WHERE name=%s AND head FOR UPDATE", (name,)).fetchone()
        refs = tuple(row["source_ref"] for row in conn.execute(
            "SELECT source_ref FROM media_sources WHERE name=%s ORDER BY revision",
            (name,)).fetchall())
        return head, refs

    def save_draft(self, draft: DraftRef, name: str, *, by: Actor) -> dict:
        """Save a draft at any time: the next revision of its `editing` name (renamed to `name`
        when it differs), or revision 1 (or the next hidden revision) of a new `name`. The new
        revision carries the draft's query and pool; Scenes using the edited name move to it,
        admitted Runs keep theirs; the draft goes."""
        with self.db.transaction() as conn, self.runtime.edit(
                conn, by=by, command="save_source") as runtime:
            found = self.library.draft_for_save_in(conn, draft)
            if found is None:
                raise LibraryRefused("draft_unknown", 404)
            editing = found.editing_name
            target, target_refs = self._versions_in(conn, name)
            if editing is None or editing != name:
                self._name(name)
                if target is not None and not target["deleted"]:
                    raise RegistryError("source_name_exists", 409)
            revised: tuple[str, ...] = target_refs
            if editing is not None and editing != name:
                head, revised = self._versions_in(conn, editing)
                if head is None or head["deleted"]:
                    raise RegistryError("source_not_found", 404)
                conn.execute("UPDATE media_sources SET deleted=TRUE,renamed_to=%s,"
                             "changed_at=%s,changed_by=%s WHERE name=%s AND head",
                             (name, self.repository.times.now_in(conn), by, editing))
            elif editing is not None and (target is None or target["deleted"]):
                raise RegistryError("source_not_found", 404)
            revision = 1 if target is None else target["revision"] + 1
            source_ref = f"{name}:{revision}"
            self._configure_query_in(conn, name, revision, source_ref, found.query, found.pool_key,
                                     by=by)
            if revised:
                runtime.revise_source_refs(set(revised), source_ref)
            self.library.remove_draft_in(conn, draft)
            self.repository.reconcile_source_activity_in(conn, runtime.planning_source_refs())
            return {"name": name, "revision": revision, "source_ref": source_ref, "created": True}

    def _configure_query_in(self, conn, name: str, revision: int, source_ref: str,
                            query: SourceQuery, pool_key: PoolKey, *, by: Actor) -> None:
        """A Photo source revision (its query and pool), its name's head from now."""
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
        if revision == 1 and conn.execute(
                "SELECT count(*) AS n FROM media_sources WHERE head AND NOT deleted"
        ).fetchone()["n"] >= self.repository.limits.max_sources:
            raise RegistryError("source_limit", 409)
        now = self.repository.times.now_in(conn)
        # The head moves: off the name's last revision, onto this one.
        conn.execute("UPDATE media_sources SET head=FALSE,deleted=FALSE,renamed_to=NULL,"
                     "changed_at=%s,changed_by=%s WHERE name=%s AND head", (now, by, name))
        conn.execute("INSERT INTO media_sources(source_ref,query,pool_key,name,revision,head,"
                     "created_at,created_by,changed_at,changed_by) "
                     "VALUES(%s,%s,%s,%s,%s,TRUE,%s,%s,%s,%s)",
                     (source_ref, Jsonb(query.model_dump(mode="json")), pool_key, name, revision,
                      now, by, now, by))

    def delete(self, name: str, expected_revision: int, *, by: Actor) -> dict:
        with self.db.transaction() as conn, self.runtime.edit(
                conn, by=by, command="delete_source") as runtime:
            head, refs = self._versions_in(conn, name)
            if head is None:
                raise RegistryError("source_not_found", 404)
            if head["revision"] != expected_revision:
                raise RegistryError("source_revision_conflict", 409)
            if head["deleted"]:
                return {"name": name, "revision": head["revision"], "deleted": False}
            dependent = runtime.scenes_using_sources(set(refs))
            if dependent:
                raise SourceInUse(dependent)
            conn.execute("UPDATE media_sources SET deleted=TRUE,changed_at=%s,changed_by=%s "
                         "WHERE name=%s AND head", (self.repository.times.now_in(conn), by, name))
            self.repository.reconcile_source_activity_in(conn, runtime.planning_source_refs())
            return {"name": name, "revision": head["revision"], "deleted": True}
