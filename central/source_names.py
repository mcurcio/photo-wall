"""Logical operator Source names over immutable media query revisions.

A Photo source is saved from a draft (`save_draft`), as the next revision of its name with the
draft's query and pool (one pool per distinct query, so the pool carries over); a rename is a
draft saved under a new name. This service is the one home that writes a revision and its name
(`media_sources`, `media_source_names`, `media_source_name_versions`).
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
        """A name's head row (locked) and every revision it has had, oldest first."""
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
        head = conn.execute("SELECT * FROM media_source_names WHERE name=%s FOR UPDATE",
                            (name,)).fetchone()
        refs = tuple(row["source_ref"] for row in conn.execute(
            "SELECT source_ref FROM media_source_name_versions WHERE name=%s ORDER BY revision",
            (name,)).fetchall())
        return head, refs

    def save_draft(self, draft: DraftRef, name: str, *, by: Actor) -> dict:
        """Save a draft at any time: the next revision of its `editing` name (renamed to `name`
        when it differs), or revision 1 (or the next hidden revision) of a new `name`. The new
        revision carries the draft's query and pool; Scenes using the edited name move to it,
        admitted Runs keep theirs; the draft goes."""
        with self.db.transaction() as conn, self.runtime.edit(conn, by=by) as runtime:
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
                conn.execute("UPDATE media_source_names SET deleted=TRUE,renamed_to=%s "
                             "WHERE name=%s", (name, editing))
            elif editing is not None and (target is None or target["deleted"]):
                raise RegistryError("source_not_found", 404)
            revision = 1 if target is None else target["revision"] + 1
            source_ref = f"{name}:{revision}"
            self._configure_query_in(conn, name, revision, source_ref, found.query, found.pool_key)
            if revised:
                runtime.revise_source_refs(set(revised), source_ref)
            self.library.remove_draft_in(conn, draft)
            self.repository.reconcile_source_activity_in(conn, runtime.planning_source_refs())
            return {"name": name, "revision": revision, "source_ref": source_ref, "created": True}

    def _configure_query_in(self, conn, name: str, revision: int, source_ref: str,
                            query: SourceQuery, pool_key: PoolKey) -> None:
        """A Photo source revision (its query and pool) and its name's head."""
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
        if revision == 1 and conn.execute(
                "SELECT count(*) AS n FROM media_source_names WHERE NOT deleted"
        ).fetchone()["n"] >= self.repository.limits.max_sources:
            raise RegistryError("source_limit", 409)
        conn.execute("INSERT INTO media_sources(source_ref,query,pool_key) VALUES(%s,%s,%s)",
                     (source_ref, Jsonb(query.model_dump(mode="json")), pool_key))
        if revision == 1:
            conn.execute("INSERT INTO media_source_names(name,current_ref,revision) VALUES(%s,%s,%s)",
                         (name, source_ref, revision))
        else:
            conn.execute("UPDATE media_source_names SET current_ref=%s,revision=%s,deleted=FALSE,"
                         "renamed_to=NULL WHERE name=%s", (source_ref, revision, name))
        conn.execute("INSERT INTO media_source_name_versions(source_ref,name,revision) "
                     "VALUES(%s,%s,%s)", (source_ref, name, revision))

    def delete(self, name: str, expected_revision: int, *, by: Actor) -> dict:
        with self.db.transaction() as conn, self.runtime.edit(conn, by=by) as runtime:
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
            conn.execute("UPDATE media_source_names SET deleted=TRUE WHERE name=%s", (name,))
            self.repository.reconcile_source_activity_in(conn, runtime.planning_source_refs())
            return {"name": name, "revision": head["revision"], "deleted": True}
