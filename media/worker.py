"""Central-only media worker; secrets and upstream facts never enter Player delivery."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Final, Protocol

from pydantic import Field

from central import cache_layout
from central.assets.layout import TEMP_PREFIX, CacheLayout
from central.content_wiring import build_job_runtime
from central.db import Database, DatabaseTransactionClock
from central.infra.photo_library_store import PgPhotoLibrary
from central.infra.runtime import (
    COMPLETION_NOT_RECORDED,
    WORKER_EXITED,
    JobRuntime,
    until_stopped,
)
from central.kernel.assets import AssetKind
from central.media_queue import MEDIA_QUEUE, ProcrastinateMediaQueue
from central.media_repository import JobLease, MediaRepository
from central.media_store import MediaStore, MediaStoreError
from central.node_bus_wiring import NodeBus, build_node_bus
from central.photo_library.model import ConnectionSecret, LibraryAsset
from central.photo_library.ports import LibraryWorkerStore
from central.registry import RegistryError
from contracts.models import Model, Positive
from contracts.time import Clock, SystemClock
from media.immich import ImmichClient, LibraryLimits
from media.library_checks import LibraryChecks
from media.library_client import FetchedRendition, LibraryClient
from media.library_clients import LibraryClients
from media.library_thumbnails import LibraryThumbnailOrigin
from media.models import MediaError
from media.prepare import BuildIdentity, PreparationLimits, PreparedMedia, Preparer
from media.task_queue import (
    MediaTaskFailed,
    ProcrastinateCheckQueue,
    RetryableMediaTask,
    create_worker_app,
)

logger = logging.getLogger("photo_wall.worker")


# The job runtime's process-ending conditions; any other RuntimeError stays `worker_internal`.
_RUNTIME_EXITS: Final = frozenset({WORKER_EXITED, COMPLETION_NOT_RECORDED})
_PERMANENT = frozenset({
    "asset_integrity", "asset_oversize", "unsupported_media", "unsupported_color",
    "metadata_invalid", "item_over_limits", "metadata_mismatch", "preparation_limit", "preparation_invalid",
    "media_original_mismatch", "media_result_mismatch", "media_journal_invalid",
})


class WorkerLimits(Model):
    job_seconds: Positive = Field(default=330, le=330)
    close_seconds: Positive = Field(default=5, le=10)


class PreparationClient(Protocol):
    async def describe_recipe(self) -> tuple[str, BuildIdentity]: ...
    async def prepare_rendition(self, asset: LibraryAsset, rendition: FetchedRendition,
                                destination: Path) -> PreparedMedia: ...


async def _drain(task: asyncio.Task):
    """Join cleanup work even if another cancellation arrives during shutdown."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def _blocking(function, *args, **kwargs):
    operation = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        try:
            await _drain(operation)
        except Exception:
            pass
        raise


class MediaWorker:
    def __init__(self, repository: MediaRepository, store: MediaStore,
                 library: LibraryWorkerStore, *,
                 preparer: PreparationClient | None = None,
                 client_factory: Callable[[ConnectionSecret], LibraryClient] | None = None,
                 limits: WorkerLimits = WorkerLimits(), previews: Path | None = None):
        self.repository, self.store, self.clock, self.limits = repository, store, repository.clock, limits
        if store.repository is not repository or limits.job_seconds + 30 > repository.limits.lease_seconds:
            raise MediaError("worker_config", "incompatible")
        # Roadmap 4: connections and their keys are the photo library store's (no file).
        self.library = library
        self.preparer = preparer or Preparer(clock=self.clock, limits=PreparationLimits(
            max_original_bytes=repository.limits.max_original_bytes,
            max_image_bytes=repository.limits.max_image_bytes,
            max_video_bytes=repository.limits.max_video_bytes,
            max_staging_bytes=repository.limits.max_original_bytes + max(
                repository.limits.max_image_bytes, repository.limits.max_video_bytes),
        ))
        self.client_factory = client_factory or (lambda secret: ImmichClient.for_connection(
            secret, clock=self.clock,
            limits=LibraryLimits(max_fetch_bytes=repository.limits.max_original_bytes)))
        self._clients = LibraryClients(self.client_factory, self._close)
        # The thumbnail directory, swept of files whose record maintenance retired.
        self.previews = previews
        self._error: str | None = None
        self._recipe: str | None = None

    @staticmethod
    def _code(error: BaseException) -> str:
        if isinstance(error, TimeoutError):
            return "worker_timeout"
        if isinstance(error, (MediaError, RegistryError)) and re.fullmatch(r"[a-z_]{1,64}", error.code):
            return error.code
        if isinstance(error, OSError):
            return "worker_io"
        if type(error) is RuntimeError and error.args and error.args[0] in _RUNTIME_EXITS:
            return error.args[0]
        return "worker_internal"

    def _utc(self) -> float:
        now = self.clock.utc()
        if not math.isfinite(now):
            raise MediaError("clock_invalid")
        return now

    async def _client(self, connection_ref: str) -> LibraryClient:
        """The connection's client for its address and key now (`LibraryClients`)."""
        secret = await _blocking(self.library.connection_secret, connection_ref)
        if secret is None:
            raise MediaError("connection_unknown")
        return await self._clients.for_secret(secret)

    async def _register_recipe(self):
        self._utc()
        recipe, _ = await self.preparer.describe_recipe()
        if recipe != self._recipe:
            await _blocking(self.repository.set_recipe, recipe)
            self._recipe = recipe

    async def _fail(self, lease: JobLease, code: str, *, retry: bool = True):
        try:
            await _blocking(self.store.fail, lease, code,
                            retry=code not in _PERMANENT and retry)
        except RegistryError as error:
            if error.code != "stale_job":
                self._error = self._code(error)
        except Exception as error:
            self._error = self._code(error)

    async def process_job(self, job_id: str, *, attempt: int) -> None:
        """Execute the exact job selected by Procrastinate.

        Procrastinate owns dispatch and retry timing. The media journal retains
        byte reservation and publication fencing only.
        """
        await self._register_recipe()
        lease = await _blocking(self.repository.claim_job, job_id)
        if lease is None:
            health = await _blocking(self.repository.health)
            if health["worker_error"] == "storage_pressure":
                reserve = self.repository.limits.max_original_bytes + max(
                    self.repository.limits.max_image_bytes,
                    self.repository.limits.max_video_bytes,
                )
                await _blocking(
                    self.store.collect,
                    target_bytes=max(0, health["max_bytes"] - reserve),
                )
                lease = await _blocking(self.repository.claim_job, job_id)
            if lease is None:
                raise RetryableMediaTask("media_job_unavailable")
        try:
            async with asyncio.timeout(self.limits.job_seconds):
                paths = await _blocking(self.store.staging, lease)
                client = await self._client(lease.asset.connection_ref)
                edge = await _blocking(self.library.largest_frame_edge)
                fetched = await client.fetch_rendition(lease.asset, frame_long_edge=edge,
                                                       destination=paths.original)
                if fetched.path.absolute() != paths.original:
                    raise MediaError("asset_integrity")
                prepared = await self.preparer.prepare_rendition(lease.asset, fetched,
                                                                 paths.variant)
                if prepared.original_sha256 != fetched.sha256:
                    raise MediaError("asset_integrity")
                if prepared.recipe_id != lease.recipe_id:
                    raise MediaError("recipe_changed")
                await _blocking(self.store.publish, lease, prepared)
            self._error = None
        except asyncio.CancelledError:
            self._error = "worker_cancelled"
            await _drain(asyncio.create_task(self._fail(lease, self._error, retry=attempt <= 3)))
            raise
        except Exception as error:
            self._error = self._code(error)
            retry = self._error not in _PERMANENT and attempt <= 3
            await self._fail(lease, self._error, retry=retry)
            if retry:
                raise RetryableMediaTask(self._error) from None
            raise MediaTaskFailed(self._error) from None
        finally:
            await _blocking(self.repository.worker_status, self._error)

    async def maintain(self) -> None:
        await _blocking(self.store.recover)
        # Listed BEFORE the purge: a file written after it is never discarded by this pass.
        files = await _blocking(self._thumbnail_files)
        recorded = await _blocking(self.library.retire_unwanted)
        await _blocking(self._discard_thumbnails, files, recorded)
        await _blocking(self.repository.worker_status, self._error)

    def _thumbnail_files(self) -> dict[str, Path]:
        """Thumbnail files by asset id (temps excluded: their writer removes them)."""
        if self.previews is None or not self.previews.is_dir():
            return {}
        return {path.name.removesuffix(".jpg"): path for path in self.previews.iterdir()
                if path.name.endswith(".jpg") and not path.name.startswith(TEMP_PREFIX)}

    @staticmethod
    def _discard_thumbnails(files: Mapping[str, Path], recorded: frozenset[str]) -> None:
        for asset_id, path in files.items():
            if asset_id not in recorded:
                path.unlink(missing_ok=True)

    async def _close(self, client: LibraryClient) -> None:
        try:
            async with asyncio.timeout(self.limits.close_seconds):
                await client.close()
        except Exception:
            self._error = "connection_close"

    async def _close_clients(self):
        await self._clients.close()


_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
MEDIA_STANDBY_SECONDS: Final = 5.0


async def _media_queue(app, additional_context: dict) -> None:
    """The legacy media queue itself (procrastinate, `MEDIA_QUEUE` only)."""
    await app.run_worker_async(
        queues=[MEDIA_QUEUE],
        concurrency=4,
        additional_context=additional_context,
        install_signal_handlers=False,
    )


async def _media_writer(worker: MediaWorker, run_queue: Callable[[], Awaitable[None]], *,
                        standby_seconds: float = MEDIA_STANDBY_SECONDS) -> None:
    """The legacy media loop, run only while this process holds the media writer lock.

    The lock guards the media store's single writer, NOT the job runtime (design §2: every
    worker process runs the job runtime). While another process holds it, this one stands by,
    retrying every `standby_seconds`, and takes over when the holder exits.
    """
    with contextlib.ExitStack() as held:
        while True:
            try:
                held.enter_context(worker.store.worker_lock())
                break
            except MediaStoreError as error:
                if error.code != "media_writer_active":
                    raise
            await asyncio.sleep(standby_seconds)
        await worker._register_recipe()
        await worker.maintain()
        await run_queue()


async def _run_workers(media: Awaitable[None], runtime: JobRuntime,
                       bus: NodeBus | None = None) -> None:
    """The legacy media loop, the job runtime and, with a hub deployed, the Node bus side by side,
    under ONE signal handler.

    SIGTERM/SIGINT stops each gracefully (running jobs finish), then `_entry` returns and the
    process exits 0. None installs its own handlers. A loop that fails, or ends without being
    asked to stop (`until_stopped`), cancels the others and the process exits non-zero.
    """
    loop = asyncio.get_running_loop()
    stopping = False
    bus_stop = asyncio.Event()

    def stopped() -> bool:
        return stopping

    try:
        async with asyncio.TaskGroup() as group:
            media_task = group.create_task(until_stopped(media, stopped), name="worker media")
            group.create_task(until_stopped(runtime.run(), stopped), name="worker jobs")
            if bus is not None:
                group.create_task(until_stopped(bus.run(bus_stop), stopped), name="worker node bus")

            def stop() -> None:
                nonlocal stopping
                stopping = True
                runtime.stop()
                bus_stop.set()
                media_task.cancel()  # procrastinate stops a cancelled worker gracefully

            for number in _STOP_SIGNALS:
                loop.add_signal_handler(number, stop)
    finally:
        for number in _STOP_SIGNALS:
            loop.remove_signal_handler(number)


def build_repository(dsn: str, clock: Clock | None = None, *,
                     queue: ProcrastinateMediaQueue | None = None) -> MediaRepository:
    """The worker's MediaRepository: the worker entry and `media.healthcheck` both compose here.

    G11: media times on the database's clock in every worker process; `clock` stays for the
    worker's monotonic budgets and process-local decisions.
    """
    return MediaRepository(Database(dsn), clock or SystemClock(),
                           times=DatabaseTransactionClock(), queue=queue)


async def _entry():
    try:
        dsn = os.environ["PHOTO_WALL_DATABASE_URL"]
    except KeyError:
        raise MediaError("worker_config", "incompatible") from None
    # 0013: the media store lives at the media/ subdir of the one cache root
    # (PHOTO_WALL_CACHE_ROOT, baked default) -- derived internally, no per-domain
    # env.
    root = cache_layout.media_root()
    clock = SystemClock()
    repository = build_repository(dsn, clock, queue=ProcrastinateMediaQueue(dsn))
    db = repository.db
    # Roadmap 4: connections, their keys and the photo pools live in Central's database; the
    # checks read Immich through ImmichClient.for_connection (run ledger slice M2).
    library = PgPhotoLibrary(db, times=DatabaseTransactionClock(),
                             queue=ProcrastinateMediaQueue(dsn))
    cache_root = cache_layout.cache_root()
    previews = ensure_previews_directory(cache_root)
    worker = MediaWorker(repository, MediaStore(repository, Path(root)), library,
                         previews=previews)
    # The library half of the thumbnail fetch (R22): only this package holds the key.
    thumbnails = LibraryThumbnailOrigin(
        library.pool_thumbnail, library.connection_secret,
        lambda secret: ImmichClient.for_connection(secret, clock=clock))
    # The one worker kind (design §10.5): every asset handler behind JobRuntime, in EVERY worker
    # process; only the legacy media loop is single-writer. Its boot checks run here, so a wiring
    # mistake fails before any job is claimed.
    runtime = build_job_runtime(db, clock, cache_root=cache_root, env=os.environ,
                                thumbnails=thumbnails)
    await _blocking(repository.db.migrate)
    await _blocking(ProcrastinateMediaQueue.apply_schema, dsn)
    app = create_worker_app(dsn)
    checks = LibraryChecks(
        library, lambda secret: ImmichClient.for_connection(secret, clock=clock), clock,
        queue=ProcrastinateCheckQueue(app))
    additional_context = {"media_worker": worker, "library_checks": checks}
    try:
        async with app.open_async():
            await _run_workers(
                _media_writer(worker, lambda: _media_queue(app, additional_context)), runtime,
                build_node_bus(dsn, os.environ))
    finally:
        await worker._close_clients()
        await checks.close()
        await thumbnails.aclose()


def ensure_previews_directory(cache_root: Path) -> Path:
    """ADR 0013's fourth subdirectory, created on a volume made before it existed (0700, as the
    entrypoint and the image create the others)."""
    previews = CacheLayout(cache_root).directory(AssetKind.LIBRARY_THUMBNAIL)
    previews.mkdir(mode=0o700, parents=True, exist_ok=True)
    return previews


def main() -> int:
    try:
        asyncio.run(_entry())
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        while isinstance(error, ExceptionGroup):  # the loop that failed first, not the group
            error = error.exceptions[0]
        print(json.dumps({"error": MediaWorker._code(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
