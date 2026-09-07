"""Small transaction-bound media interfaces consumed by coordination.

The coordinator supplies its existing PostgreSQL transaction. Media acquires
its lock and remains the only owner of media persistence details.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from central.catalog import Candidate, CatalogSnapshot
from contracts.models import Variant


@dataclass(frozen=True, slots=True)
class MediaPin:
    owner: str
    variant: Variant
    expires_at: float


class CoordinationMedia(Protocol):
    """Media capabilities needed by execution coordination."""

    def catalog_in(
        self, conn: Any, now: float,
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
