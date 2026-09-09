"""Shared, bounded wire contract for the runtime source audit."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from scripts.harness_failure import PROVENANCE_FAILURE_STAGES, CodedFailure

MAX_PROVENANCE_BYTES = 1024 * 1024
CORE_PACKAGES = ("central", "media", "contracts", "player")
SHA256 = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$")]


def relative_source_path(value: str) -> str:
    path = PurePosixPath(value)
    if (value in (".", "..") or path.is_absolute() or path.as_posix() != value
            or any(part in (".", "..") for part in path.parts)):
        raise ValueError("provenance_path")
    return value


SourcePath = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_./-]+$"),
    AfterValidator(relative_source_path),
]
SourceInventory = Annotated[dict[SourcePath, SHA256], Field(min_length=1, max_length=4096)]


def inventory_sha256(files: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProvenanceFailure(StrictModel):
    type: Literal["photo_wall_provenance_failure"]
    schema_version: Annotated[int, Field(alias="schema", ge=1, le=1)]
    stage: Literal["manifest", "application", "bundle", "result", "internal"]
    code: Literal[tuple(PROVENANCE_FAILURE_STAGES)]

    @model_validator(mode="after")
    def matching_stage(self) -> Self:
        if PROVENANCE_FAILURE_STAGES[self.code] != self.stage:
            raise ValueError("provenance_failure_stage")
        return self


class ProvenanceCollectionError(CodedFailure):
    def __init__(self, failure: ProvenanceFailure, role: str | None = None):
        super().__init__(failure.code)
        if role not in (None, "central", "worker"):
            raise ValueError("provenance_failure_role")
        self.failure = failure
        self.role = role

    @classmethod
    def from_code(cls, code: str) -> Self:
        return cls(ProvenanceFailure.model_validate({
            "type": "photo_wall_provenance_failure", "schema": 1,
            "stage": PROVENANCE_FAILURE_STAGES[code], "code": code,
        }))


class HelperBundleManifest(StrictModel):
    schema_version: Annotated[int, Field(alias="schema", ge=1, le=1)]
    files: SourceInventory


class RuntimeProvenance(StrictModel):
    schema_version: Annotated[int, Field(alias="schema", ge=1, le=1)]
    files: SourceInventory
    core_inventory_sha256: SHA256
    adapter_sha256: SHA256
    preparer_sha256: SHA256
    harness_sha256: SHA256
    helper_bundle: HelperBundleManifest

    @model_validator(mode="after")
    def consistent_hashes(self) -> Self:
        if (self.core_inventory_sha256 != inventory_sha256(self.files)
                or self.adapter_sha256 != self.files.get("media/immich.py")
                or self.preparer_sha256 != self.files.get("media/prepare.py")
                or self.harness_sha256 != self.helper_bundle.files.get("demo_wall.py")):
            raise ValueError("provenance_hash_mismatch")
        return self


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("provenance_duplicate_key")
        result[key] = value
    return result


def bounded_json(data: str | bytes) -> object:
    if not isinstance(data, (str, bytes)) or len(data) > MAX_PROVENANCE_BYTES:
        raise ValueError("provenance_bound")
    if isinstance(data, str) and len(data.encode()) > MAX_PROVENANCE_BYTES:
        raise ValueError("provenance_bound")
    return json.loads(data, object_pairs_hook=_unique_object)


def decode_provenance(data: str | bytes) -> RuntimeProvenance:
    return RuntimeProvenance.model_validate(bounded_json(data))


def decode_provenance_failure(data: str | bytes) -> ProvenanceFailure:
    if len(data) > 2048:
        raise ValueError("provenance_failure_bound")
    return ProvenanceFailure.model_validate(bounded_json(data))
