"""Job-runtime test support: stub handlers for the whole catalog, and the procrastinate schema
installer the DB tests share.

Handler classes import their job types at runtime (never under TYPE_CHECKING): the runtime reads
`handle`'s annotations to dispatch.
"""

from __future__ import annotations

import asyncio
from typing import Any

import procrastinate
import psycopg

from central.kernel.assets import AssetReady, MediaReady
from central.kernel.job_types import (
    CATALOG,
    FetchLibraryThumbnail,
    FetchOsImage,
    FetchSealedEnvironment,
    MaintainCache,
    Prefetch,
    PrepareMedia,
    PurgeFinishedJobs,
    RescueStalledJobs,
    SyncReleases,
)
from central.kernel.jobs import Job

FACTS = AssetReady(size=7, sha256="ef" * 32)
MEDIA_FACTS = MediaReady(size=7, sha256="ef" * 32, media_type="image/jpeg", width=4, height=3,
                         duration=None, made_from="preview")
MEDIA_KEY = "asset-" + "56" * 32 + "." + "78" * 32


class FetchOsImageStub:
    async def handle(self, job: FetchOsImage) -> AssetReady:
        return FACTS


class FetchSealedEnvironmentStub:
    async def handle(self, job: FetchSealedEnvironment) -> AssetReady:
        return FACTS


class FetchLibraryThumbnailStub:
    async def handle(self, job: FetchLibraryThumbnail) -> AssetReady:
        return FACTS


class PrepareMediaStub:
    async def handle(self, job: PrepareMedia) -> MediaReady:
        return MEDIA_FACTS


class SyncReleasesStub:
    async def handle(self, job: SyncReleases) -> None:
        return None


class PrefetchStub:
    async def handle(self, job: Prefetch) -> None:
        return None


class MaintainCacheStub:
    async def handle(self, job: MaintainCache) -> None:
        return None


class RescueStub:
    async def handle(self, job: RescueStalledJobs) -> None:
        return None


class PurgeStub:
    async def handle(self, job: PurgeFinishedJobs) -> None:
        return None


def catalog_stubs() -> list[Any]:
    return [FetchOsImageStub(), FetchSealedEnvironmentStub(),
            FetchLibraryThumbnailStub(), PrepareMediaStub(), SyncReleasesStub(),
            PrefetchStub(), MaintainCacheStub(), RescueStub(), PurgeStub()]


def catalog_instances() -> list[Job]:
    samples = {FetchOsImage: FetchOsImage(tarball_sha256="cd" * 32),
               FetchSealedEnvironment: FetchSealedEnvironment(sha256="12" * 32),
               FetchLibraryThumbnail: FetchLibraryThumbnail(asset_id="asset-" + "34" * 32),
               PrepareMedia: PrepareMedia(key=MEDIA_KEY)}
    return [samples.get(job_type) or job_type() for job_type in CATALOG]


def apply_procrastinate_schema(dsn: str) -> None:
    """Install procrastinate's schema into the (test-private) schema `dsn` searches, once: a
    database that already has it is left as it is (several seeding helpers may ask)."""
    with psycopg.connect(dsn) as conn:
        if conn.execute("SELECT to_regclass('procrastinate_jobs')").fetchone()[0] is not None:
            return
    app = procrastinate.App(connector=procrastinate.SyncPsycopgConnector(conninfo=dsn))
    app.open()
    try:
        app.schema_manager.apply_schema()
    finally:
        app.close()


class NoNames:
    """NamedReferences that never names a reference: follows until cancelled."""

    async def follow(self, named) -> None:
        await asyncio.Event().wait()


class _UnheardHealth:
    """A link health that keeps no beat."""

    def beat(self, beat: str) -> None:
        pass


def unconfigured_link():
    """The worker's Home Assistant link as it runs with no broker configured: it only beats
    "not-configured" (here, to nothing) until stopped, and has no pool to close."""
    from central.infra.home_assistant.link import HomeAssistantLink

    return HomeAssistantLink(None, None, _UnheardHealth(), NoNames(), close=lambda: None)  # type: ignore[arg-type]
