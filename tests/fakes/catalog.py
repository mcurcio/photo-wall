"""A `ContentCatalog` answered from a fixed mapping."""

from __future__ import annotations

from collections.abc import Mapping

from central.kernel.job_types import AssetJob
from central.kernel.ports import ContentRequest, Resolution, Unknown


class StaticContentCatalog:
    """Implements `ContentCatalog`; an unmapped request resolves to `Unknown("unknown")`."""

    def __init__(self, resolutions: Mapping[ContentRequest, Resolution],
                 desired: frozenset[AssetJob] = frozenset()) -> None:
        self._resolutions = dict(resolutions)
        self._desired = desired

    async def resolve(self, request: ContentRequest) -> Resolution:
        return self._resolutions.get(request, Unknown("unknown"))

    async def desired_assets(self) -> frozenset[AssetJob]:
        return self._desired
