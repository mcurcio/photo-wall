"""Logical operator Source names over immutable media query revisions."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, StrictBool

from central.db import Database
from central.media_repository import MediaRepository
from central.registry import RegistryError
from central.runtime_store import RuntimeStore
from contracts.models import Identifier, Instant, Model
from media.models import SourceSpec

NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")


class NamedSourceWrite(Model):
    expected_revision: int | None = Field(ge=1)
    new_name: str | None = None
    connection_ref: Identifier
    favorites: StrictBool | None = None
    captured_from: Instant | None = None
    captured_until: Instant | None = None
    media_types: tuple[Literal["image", "video"], ...] = ("image", "video")

    def spec(self, source_ref: str) -> SourceSpec:
        return SourceSpec(source_ref=source_ref, connection_ref=self.connection_ref,
                          favorites=self.favorites, captured_from=self.captured_from,
                          captured_until=self.captured_until, media_types=self.media_types)


class SourceInUse(RegistryError):
    def __init__(self, scene_ids: tuple[str, ...]):
        super().__init__("source_in_use", 409)
        self.scene_ids = scene_ids


class SourceNameService:
    """Coordinate Source edits under Coordination, Runtime, then media locks."""

    def __init__(self, db: Database, repository: MediaRepository, runtime: RuntimeStore):
        self.db, self.repository, self.runtime = db, repository, runtime

    @staticmethod
    def _name(name: str) -> None:
        if NAME_PATTERN.fullmatch(name) is None:
            raise RegistryError("invalid_source_name", 422)

    @staticmethod
    def _same(spec: dict, wanted: SourceSpec) -> bool:
        return spec == wanted.model_dump(mode="json", by_alias=True)

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
