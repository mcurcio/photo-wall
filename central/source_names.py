"""Logical operator Source names over immutable media query revisions.

Roadmap 4 (slice C2): a Photo source is saved from a draft (`save_draft`), as the next revision
of its name with the draft's query and pool (one pool per distinct query, so the pool carries
over). `put` stays for pre-4 spec writes until the integrate slice retires it.
"""

from __future__ import annotations

import re
from typing import Literal

from psycopg.types.json import Jsonb
from pydantic import Field, StrictBool

from central.db import MEDIA_LOCK, Database
from central.infra.photo_library_store import PgPhotoLibrary
from central.media_repository import MediaRepository
from central.photo_library.model import DraftRef, PoolKey, SourceQuery
from central.photo_library.ports import LibraryRefused
from central.registry import RegistryError
from central.runtime_store import RuntimeStore
from contracts.models import Identifier, Instant, Model
from media.models import MAX_SOURCE_TAGS, SourceSpec, TagRef

NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")


class NamedSourceWrite(Model):
    expected_revision: int | None = Field(ge=1)
    new_name: str | None = None
    connection_ref: Identifier
    favorites: StrictBool | None = None
    captured_from: Instant | None = None
    captured_until: Instant | None = None
    media_types: tuple[Literal["image", "video"], ...] = ("image", "video")
    tags: tuple[TagRef, ...] = Field(default=(), max_length=MAX_SOURCE_TAGS)

    def spec(self, source_ref: str) -> SourceSpec:
        return SourceSpec(source_ref=source_ref, connection_ref=self.connection_ref,
                          favorites=self.favorites, captured_from=self.captured_from,
                          captured_until=self.captured_until, media_types=self.media_types,
                          tags=self.tags)


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
    def _same(spec: dict, wanted: SourceSpec) -> bool:
        return MediaRepository.same_spec(spec, wanted)

    def put(self, name: str, request: NamedSourceWrite) -> dict:
        target_name = request.new_name or name
        if target_name != name and request.expected_revision is None:
            raise RegistryError("invalid_source_rename", 422)
        with self.db.transaction() as conn, self.runtime.edit(conn) as runtime:
            head, refs = self.repository.named_versions_in(conn, name)
            # Existing exceptional legacy names remain manageable. Only names
            # created by this API (including rename targets) use the new rule.
            if head is None or target_name != name:
                self._name(target_name)
            if target_name != name:
                if head is None:
                    raise RegistryError("source_not_found", 404)
                if head["deleted"]:
                    if head["renamed_to"] == target_name:
                        target, _ = self.repository.named_versions_in(conn, target_name)
                        if target is not None and not target["deleted"]:
                            current = conn.execute("SELECT spec FROM media_sources WHERE source_ref=%s",
                                                   (target["current_ref"],)).fetchone()["spec"]
                            if self._same(current, request.spec(target["current_ref"])):
                                return {"name": target_name, "revision": target["revision"],
                                        "source_ref": target["current_ref"], "created": False}
                    raise RegistryError("source_not_found", 404)
                if head["revision"] != request.expected_revision:
                    raise RegistryError("source_revision_conflict", 409)
                target, _ = self.repository.named_versions_in(conn, target_name)
                if target is not None and not target["deleted"]:
                    raise RegistryError("source_name_exists", 409)
                revision = 1 if target is None else target["revision"] + 1
                source_ref = f"{target_name}:{revision}"
                self.repository.rename_named_in(conn, name, target_name)
                self.repository.configure_named_in(conn, target_name, revision,
                                                   request.spec(source_ref))
                runtime.revise_source_refs(set(refs), source_ref)
                self.repository.reconcile_source_activity_in(conn, runtime.planning_source_refs())
                return {"name": target_name, "revision": revision,
                        "source_ref": source_ref, "created": True}
            if request.expected_revision is None:
                if head is not None and not head["deleted"]:
                    current = conn.execute("SELECT spec FROM media_sources WHERE source_ref=%s",
                                           (head["current_ref"],)).fetchone()["spec"]
                    if self._same(current, request.spec(head["current_ref"])):
                        return {"name": name, "revision": head["revision"],
                                "source_ref": head["current_ref"], "created": False}
                    raise RegistryError("source_name_exists", 409)
                revision = 1 if head is None else head["revision"] + 1
            else:
                if head is None:
                    raise RegistryError("source_not_found", 404)
                if head["deleted"]:
                    raise RegistryError("source_not_found", 404)
                if head["revision"] != request.expected_revision:
                    # A lost response to the immediately preceding edit is safe
                    # to retry with its original expected revision and body.
                    if head["revision"] == request.expected_revision + 1:
                        current = conn.execute("SELECT spec FROM media_sources WHERE source_ref=%s",
                                               (head["current_ref"],)).fetchone()["spec"]
                        if self._same(current, request.spec(head["current_ref"])):
                            return {"name": name, "revision": head["revision"],
                                    "source_ref": head["current_ref"], "created": False}
                    raise RegistryError("source_revision_conflict", 409)
                current = conn.execute("SELECT spec FROM media_sources WHERE source_ref=%s",
                                       (head["current_ref"],)).fetchone()["spec"]
                if self._same(current, request.spec(head["current_ref"])):
                    return {"name": name, "revision": head["revision"],
                            "source_ref": head["current_ref"], "created": False}
                revision = head["revision"] + 1
            source_ref = f"{name}:{revision}"
            spec = request.spec(source_ref)
            self.repository.configure_named_in(conn, name, revision, spec)
            if refs:
                runtime.revise_source_refs(set(refs), source_ref)
            self.repository.reconcile_source_activity_in(conn, runtime.planning_source_refs())
            return {"name": name, "revision": revision, "source_ref": source_ref,
                    "created": True}

    def save_draft(self, draft: DraftRef, name: str) -> dict:
        """Save a draft at any time: the next revision of its `editing` name (renamed to `name`
        when it differs), or revision 1 (or the next hidden revision) of a new `name`. The new
        revision carries the draft's query and pool; Scenes using the edited name move to it,
        admitted Runs keep theirs (as `put`); the draft goes. Refusals are `put`'s codes."""
        with self.db.transaction() as conn, self.runtime.edit(conn) as runtime:
            found = self.library.draft_for_save_in(conn, draft)
            if found is None:
                raise LibraryRefused("draft_unknown", 404)
            editing = found.editing_name
            target, target_refs = self.repository.named_versions_in(conn, name)
            if editing is None or editing != name:
                self._name(name)
                if target is not None and not target["deleted"]:
                    raise RegistryError("source_name_exists", 409)
            revised: tuple[str, ...] = target_refs
            if editing is not None and editing != name:
                head, revised = self.repository.named_versions_in(conn, editing)
                if head is None or head["deleted"]:
                    raise RegistryError("source_not_found", 404)
                self.repository.rename_named_in(conn, editing, name)
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
        """A Photo source revision (query and pool, no pre-4 spec) and its name's head."""
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))
        if revision == 1 and conn.execute(
                "SELECT count(*) AS n FROM media_source_names WHERE NOT deleted"
        ).fetchone()["n"] >= self.repository.limits.max_sources:
            raise RegistryError("source_limit", 409)
        conn.execute("INSERT INTO media_sources(source_ref,spec,query,pool_key) VALUES(%s,NULL,%s,%s)",
                     (source_ref, Jsonb(query.model_dump(mode="json")), pool_key))
        if revision == 1:
            conn.execute("INSERT INTO media_source_names(name,current_ref,revision) VALUES(%s,%s,%s)",
                         (name, source_ref, revision))
        else:
            conn.execute("UPDATE media_source_names SET current_ref=%s,revision=%s,deleted=FALSE,"
                         "renamed_to=NULL WHERE name=%s", (source_ref, revision, name))
        conn.execute("INSERT INTO media_source_name_versions(source_ref,name,revision) "
                     "VALUES(%s,%s,%s)", (source_ref, name, revision))

    def delete(self, name: str, expected_revision: int) -> dict:
        with self.db.transaction() as conn, self.runtime.edit(conn) as runtime:
            head, refs = self.repository.named_versions_in(conn, name)
            if head is None:
                raise RegistryError("source_not_found", 404)
            if head["revision"] != expected_revision:
                raise RegistryError("source_revision_conflict", 409)
            if head["deleted"]:
                return {"name": name, "revision": head["revision"], "deleted": False}
            dependent = runtime.scenes_using_sources(set(refs))
            if dependent:
                raise SourceInUse(dependent)
            self.repository.delete_named_in(conn, name)
            self.repository.reconcile_source_activity_in(conn, runtime.planning_source_refs())
            return {"name": name, "revision": head["revision"], "deleted": True}
