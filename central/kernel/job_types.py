"""The MVP job catalog. `JobRuntime` requires exactly one handler for each type in `CATALOG`.

Test-only job types are defined at test-module scope with a `test.` name prefix; they never enter
`CATALOG`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Final, TypeAlias

from central.kernel.assets import AssetKind, AssetReady
from central.kernel.jobs import Delivery, Job, QueueName
from central.kernel.types import LibraryAssetId, Sha256

_FETCH_RETRY = (timedelta(seconds=5), timedelta(minutes=1), timedelta(minutes=5))
# A preview tile is picked after every queued boot or package fetch on the shared FETCH queue
# (procrastinate picks `priority DESC, id ASC`). Pick order only: a tile already running still
# holds a FETCH slot for up to the client's `metadata_seconds`.
_THUMBNAIL_PRIORITY = -50
# A node release file Central downloads only because its release is in the window (the newest
# stable releases), not because anything selected, offered or requested it: picked after every
# queued thumbnail, so a background download never delays a preview tile or a wanted file. A
# per-publish override (`Publisher.publish(priority=...)`), not a job type's: the same key is
# wanted at the type's priority when something selects it.
BACKGROUND_PRIORITY: Final = -60


class FetchOsImage(Job[AssetReady], name="os_image.fetch", asset=AssetKind.OS_IMAGE,
                   delivery=Delivery(queue=QueueName.FETCH, retry=_FETCH_RETRY)):
    tarball_sha256: Sha256  # the base tarball's sha256: the image is a pure function of it


class FetchPackage(Job[AssetReady], name="player_deb.fetch", asset=AssetKind.PLAYER_DEB,
                   delivery=Delivery(queue=QueueName.FETCH, retry=_FETCH_RETRY)):
    sha256: Sha256


class FetchPlayerPayload(Job[AssetReady], name="player_payload.fetch",
                         asset=AssetKind.PLAYER_PAYLOAD,
                         delivery=Delivery(queue=QueueName.FETCH, retry=_FETCH_RETRY)):
    sha256: Sha256


class FetchSealedEnvironment(Job[AssetReady], name="sealed_environment.fetch",
                             asset=AssetKind.SEALED_ENVIRONMENT,
                             delivery=Delivery(queue=QueueName.FETCH, retry=_FETCH_RETRY)):
    sha256: Sha256


class FetchLibraryThumbnail(Job[AssetReady], name="library_thumbnail.fetch",
                            asset=AssetKind.LIBRARY_THUMBNAIL,
                            delivery=Delivery(queue=QueueName.FETCH, retry=_FETCH_RETRY,
                                              priority=_THUMBNAIL_PRIORITY)):
    """One preview tile; its handler's library half is injected by the media worker (R22).

    Below every other FETCH job's priority: a queued OS image, package or payload fetch is
    always picked first. A running tile still holds a FETCH slot for its attempt (at most
    the client's metadata budget, `ImmichClient.thumbnail`).
    """

    asset_id: LibraryAssetId


class SyncReleases(Job[None], name="releases.sync",
                   delivery=Delivery(queue=QueueName.FETCH, every=timedelta(minutes=15))):
    pass


class Prefetch(Job[None], name="assets.prefetch",
               delivery=Delivery(queue=QueueName.UPKEEP, every=timedelta(minutes=5))):
    pass


class MaintainCache(Job[None], name="assets.maintain_cache",
                    delivery=Delivery(queue=QueueName.UPKEEP, every=timedelta(hours=1))):
    """Remove cached release files nothing wants (`central/assets/maintenance.py`)."""


class RescueStalledJobs(Job[None], name="queue.rescue_stalled",
                        delivery=Delivery(queue=QueueName.UPKEEP, every=timedelta(minutes=1))):
    pass


class PurgeFinishedJobs(Job[None], name="queue.purge_finished",
                        delivery=Delivery(queue=QueueName.UPKEEP, every=timedelta(hours=1))):
    pass


AssetJob: TypeAlias = (FetchOsImage | FetchPackage | FetchPlayerPayload | FetchSealedEnvironment
                       | FetchLibraryThumbnail)
CATALOG: Final[tuple[type[Job[Any]], ...]] = (
    FetchOsImage, FetchPackage, FetchPlayerPayload, FetchSealedEnvironment, FetchLibraryThumbnail,
    SyncReleases, Prefetch, MaintainCache, RescueStalledJobs, PurgeFinishedJobs)
