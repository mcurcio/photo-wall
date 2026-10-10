"""Fleet domain values: the exact bytes an offer names, and the fleet's HTTP-mapped error."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OfferAsset:
    """Frozen exact bytes a node boot offer or a node app command names."""

    kind: str
    tag: str
    content_key: str
    sha256: str
    size: int
    format: str | None = None


class FleetError(Exception):
    def __init__(self, code: str, status: int = 409, *, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.code, self.status, self.retry_after = code, status, retry_after
