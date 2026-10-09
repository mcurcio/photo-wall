"""A `ContentCatalog` answered from a fixed mapping."""

from __future__ import annotations

from collections.abc import Mapping

from central.kernel.job_types import AssetJob
from central.kernel.ports import DesiredTiers, PackageRequest, Resolution, Unknown


class StaticContentCatalog:
    """Implements `ContentCatalog`; an unmapped request resolves to `Unknown("unknown")`."""

    def __init__(self, resolutions: Mapping[PackageRequest, Resolution],
                 desired: frozenset[AssetJob] = frozenset(),
                 background: tuple[AssetJob, ...] = ()) -> None:
        self._resolutions = dict(resolutions)
        self._desired = desired
        self._background = background

    async def resolve(self, request: PackageRequest) -> Resolution:
        return self._resolutions.get(request, Unknown("unknown"))

    async def desired_assets(self) -> frozenset[AssetJob]:
        return self._desired | frozenset(self._background)

    async def desired_tiers(self) -> DesiredTiers:
        return DesiredTiers(self._desired, self._background)
