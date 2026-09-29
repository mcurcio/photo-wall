"""Small central media interfaces for coordination and application operations.

Coordination supplies its existing PostgreSQL transaction. Application callers
use named operations. Media remains the only owner of media persistence details.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import Field, StrictBool, model_validator

from central.catalog import Candidate, CatalogSnapshot
from central.planner import AcquisitionRequest
from contracts.models import FrameProfile, Identifier, Model, Variant
from media.models import SourcePreviewQuery, SourceSpec


@dataclass(frozen=True, slots=True)
class MediaPin:
    owner: str
    variant: Variant
    expires_at: float


class RefreshReceipt(Model):
    source_ref: Identifier
    requested_revision: int = Field(ge=1)
    completed_revision: int = Field(ge=0)
    coalesced: bool

    @model_validator(mode="after")
    def request_is_pending(self):
        if self.completed_revision >= self.requested_revision:
            raise ValueError("completed_revision must precede requested_revision")
        return self


class SourceConfigurationReceipt(Model):
    """Exact operator transport result for a configured source."""

    source_ref: Identifier
    created: StrictBool


class SourcePreviewReceipt(Model):
    request_id: Identifier
    status: Literal["pending"] = "pending"


class CoordinationMedia(Protocol):
    """Media capabilities needed by execution coordination."""

    def reconcile_source_activity_in(self, conn: Any, runtime_refs: set[str]) -> None: ...

    def catalog_in(
        self, conn: Any, now: float, source_refs: set[str] | None = None,
    ) -> tuple[dict[str, CatalogSnapshot], dict[str, Candidate]]: ...

    def authored_candidates_in(
        self, conn: Any, asset_ids: tuple[str, ...],
    ) -> dict[str, Candidate]: ...

    def author_candidates_in(
        self, conn: Any, source_ref: str, asset_ids: tuple[str, ...],
    ) -> dict: ...

    def pin_variants_in(
        self, conn: Any, pins: Iterable[MediaPin], *, require_ready: bool,
    ) -> None: ...

    def expire_pins_in(self, conn: Any, now: float) -> None: ...


class MediaApplication(Protocol):
    """Central application operations exposed to schedulers and operators."""

    def request_acquisitions(self, requests: tuple[AcquisitionRequest, ...]) -> int: ...

    def configure_source(self, spec: SourceSpec) -> bool: ...

    def request_refresh(self, source_ref: str) -> RefreshReceipt: ...

    def request_source_preview(self, query: SourcePreviewQuery) -> SourcePreviewReceipt: ...

    def source_preview(self, request_id: str) -> dict: ...

    def sources(self) -> list[dict]: ...

    def source_candidates(
        self, source_ref: str, *, profile: FrameProfile | None = None,
    ) -> dict: ...

    def author_authored_candidates(
        self, source_ref: str, asset_ids: tuple[str, ...],
    ) -> dict: ...

    def health(self) -> dict: ...
