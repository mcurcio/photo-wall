"""A `ContentCatalog` answered from a fixed desired set."""

from __future__ import annotations

from central.kernel.job_types import AssetJob
from central.kernel.ports import DesiredTiers


class StaticContentCatalog:
    """Implements `ContentCatalog`."""

    def __init__(self, desired: frozenset[AssetJob] = frozenset(),
                 background: tuple[AssetJob, ...] = ()) -> None:
        self._desired = desired
        self._background = background

    async def desired_assets(self) -> frozenset[AssetJob]:
        return self._desired | frozenset(self._background)

    async def desired_tiers(self) -> DesiredTiers:
        return DesiredTiers(self._desired, self._background)
