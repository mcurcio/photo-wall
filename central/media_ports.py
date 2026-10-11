"""Small central media interfaces for coordination and application operations.

Coordination supplies its existing PostgreSQL transaction. Application callers
use named operations. Media remains the only owner of media persistence details.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from central.catalog import Candidate, SourcePool
from central.planner import AcquisitionRequest, MeasureRequest
from central.wall_plan.fit import DEFAULT_CROP_LIMIT
from contracts.models import FrameProfile, Variant


@dataclass(frozen=True, slots=True)
class MediaPin:
    owner: str
    variant: Variant
    expires_at: float


class CoordinationMedia(Protocol):
    """Media capabilities needed by execution coordination."""

    def reconcile_source_activity_in(self, conn: Any, runtime_refs: set[str]) -> None: ...

    def source_pools_in(self, conn: Any, source_refs: set[str]) -> dict[str, SourcePool]:
        """central.photo_library.ports.ScenePoolReader.source_pools_in (roadmap 4, C11)."""
        ...

    def authored_in(self, conn: Any) -> dict[str, Candidate]:
        """Every authored candidate, hydrated with its variant."""
        ...

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

    def request_measurements(self, requests: tuple[MeasureRequest, ...]) -> int: ...

    def sources(self) -> list[dict]: ...

    def media_read(self) -> dict: ...

    def source_candidates(
        self, source_ref: str, *, profile: FrameProfile | None = None,
        crop_limit: float = DEFAULT_CROP_LIMIT,
    ) -> dict: ...

    def author_authored_candidates(
        self, source_ref: str, asset_ids: tuple[str, ...],
    ) -> dict: ...

    def health(self) -> dict: ...
