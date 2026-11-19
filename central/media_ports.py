"""Small central media interfaces for coordination and application operations.

Coordination supplies its existing PostgreSQL transaction. Application callers
use named operations. Media remains the only owner of media persistence details.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Protocol

from central.catalog import Candidate
from central.planner import AcquisitionRequest, MeasureRequest
from central.wall_plan.fit import FitTarget
from central.wall_plan.fit_settings import FitSettings
from central.wall_plan.order import SceneLines
from contracts.models import Variant


class CoordinationMedia(Protocol):
    """Media capabilities needed by execution coordination."""

    def reconcile_source_activity_in(self, conn: Any, runtime_refs: set[str]) -> None: ...

    def scene_lines_in(self, conn: Any) -> SceneLines:
        """What the Planner reads of the photo library this pass, in coordination's transaction
        (follow-up S3: the top of each Scene's line, central.photo_library.ports
        .ScenePoolReader, hydrated; never a whole pool)."""
        ...

    def authored_in(self, conn: Any, asset_ids: frozenset[str]) -> dict[str, Candidate]:
        """The current facts of the photos authored Scenes name, hydrated with their variants;
        a photo no longer in the library is absent."""
        ...

    def authored_candidates_in(
        self, conn: Any, asset_ids: tuple[str, ...],
    ) -> dict[str, Candidate]: ...

    def author_candidates_in(
        self, conn: Any, source_ref: str, asset_ids: tuple[str, ...],
    ) -> dict: ...

    def require_ready_in(self, conn: Any, variants: Iterable[Variant]) -> None:
        """Refuse (`media_unavailable`) a batch naming a variant that is not ready."""
        ...

    def request_preparations_in(self, conn: Any, requests: Sequence[AcquisitionRequest]) -> int:
        """Publish `PrepareMedia` for each planned photo with no variant, in coordination's
        transaction (central/media_repository.py)."""
        ...


class MediaApplication(Protocol):
    """Central application operations exposed to schedulers and operators."""

    def request_measurements(self, requests: tuple[MeasureRequest, ...]) -> int: ...

    def sources(self) -> list[dict]: ...

    def media_read(self) -> dict: ...

    def source_candidates(
        self, source_ref: str, *, target: FitTarget | None = None,
        crop_limit: float | None = None, settings: FitSettings,
    ) -> dict: ...

    def author_authored_candidates(
        self, source_ref: str, asset_ids: tuple[str, ...],
    ) -> dict: ...

    def health(self) -> dict: ...
