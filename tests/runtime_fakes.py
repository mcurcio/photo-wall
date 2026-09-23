"""Job-runtime test support: stub handlers for the whole catalog, and the procrastinate schema
installer the DB tests share.

Handler classes import their job types at runtime (never under TYPE_CHECKING): the runtime reads
`handle`'s annotations to dispatch.
"""

from __future__ import annotations

from typing import Any

import procrastinate

from central.kernel.assets import AssetReady
from central.kernel.job_types import (
    FetchOsImage,
    FetchPackage,
    Prefetch,
    PurgeFinishedJobs,
    RescueStalledJobs,
    SyncReleases,
)

FACTS = AssetReady(size=7, sha256="ef" * 32)


class FetchOsImageStub:
    async def handle(self, job: FetchOsImage) -> AssetReady:
        return FACTS


class FetchPackageStub:
    async def handle(self, job: FetchPackage) -> AssetReady:
        return FACTS


class SyncReleasesStub:
    async def handle(self, job: SyncReleases) -> None:
        return None


class PrefetchStub:
    async def handle(self, job: Prefetch) -> None:
        return None


class RescueStub:
    async def handle(self, job: RescueStalledJobs) -> None:
        return None


class PurgeStub:
    async def handle(self, job: PurgeFinishedJobs) -> None:
        return None


def catalog_stubs() -> list[Any]:
    return [FetchOsImageStub(), FetchPackageStub(), SyncReleasesStub(), PrefetchStub(),
            RescueStub(), PurgeStub()]


def apply_procrastinate_schema(dsn: str) -> None:
    """Install procrastinate's schema into the (test-private) schema `dsn` searches."""
    app = procrastinate.App(connector=procrastinate.SyncPsycopgConnector(conninfo=dsn))
    app.open()
    try:
        app.schema_manager.apply_schema()
    finally:
        app.close()
