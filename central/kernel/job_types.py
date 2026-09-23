"""The MVP job catalog. `JobRuntime` requires exactly one handler for each type in `CATALOG`.

Test-only job types are defined at test-module scope with a `test.` name prefix; they never enter
`CATALOG`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Final, TypeAlias

from central.kernel.assets import AssetKind, AssetReady
from central.kernel.jobs import Delivery, Job, QueueName
from central.kernel.types import ReleaseTag, Sha256

_FETCH_RETRY = (timedelta(seconds=5), timedelta(minutes=1), timedelta(minutes=5))


class FetchOsImage(Job[AssetReady], name="os_image.fetch", asset=AssetKind.OS_IMAGE,
                   delivery=Delivery(queue=QueueName.FETCH, retry=_FETCH_RETRY)):
    tag: ReleaseTag


class FetchPackage(Job[AssetReady], name="player_deb.fetch", asset=AssetKind.PLAYER_DEB,
                   delivery=Delivery(queue=QueueName.FETCH, retry=_FETCH_RETRY)):
    sha256: Sha256


class SyncReleases(Job[None], name="releases.sync",
                   delivery=Delivery(queue=QueueName.FETCH, every=timedelta(minutes=15))):
    pass


class Prefetch(Job[None], name="assets.prefetch",
               delivery=Delivery(queue=QueueName.UPKEEP, every=timedelta(minutes=5))):
    pass


class RescueStalledJobs(Job[None], name="queue.rescue_stalled",
                        delivery=Delivery(queue=QueueName.UPKEEP, every=timedelta(minutes=1))):
    pass


class PurgeFinishedJobs(Job[None], name="queue.purge_finished",
                        delivery=Delivery(queue=QueueName.UPKEEP, every=timedelta(hours=1))):
    pass


AssetJob: TypeAlias = FetchOsImage | FetchPackage
CATALOG: Final[tuple[type[Job[Any]], ...]] = (
    FetchOsImage, FetchPackage, SyncReleases, Prefetch, RescueStalledJobs, PurgeFinishedJobs)
