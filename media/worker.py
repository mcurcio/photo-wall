"""Central-only media worker; secrets and upstream facts never enter Player delivery."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Final

from pydantic import Field

from central import cache_layout
from central.assets.layout import TEMP_PREFIX, CacheLayout
from central.assets.maintenance import CacheSettings, MediaCacheBudget
from central.assets.store import CacheStore
from central.content_wiring import build_job_runtime
from central.db import Database, DatabaseTransactionClock
from central.infra.home_assistant.health import PgLinkHealth
from central.infra.home_assistant.link import HomeAssistantLink, build_home_assistant_link
from central.infra.house_store import seed_house
from central.infra.key_content_store import PgKeyContent
from central.infra.media_cache_records import PgMediaVariantUse
from central.infra.photo_library_store import PgPhotoLibrary
from central.infra.room_light_store import PgRoomLights
from central.infra.runtime import (
    COMPLETION_NOT_RECORDED,
    WORKER_EXITED,
    JobRuntime,
    until_stopped,
)
from central.infra.transactions import PgTransactions
from central.kernel.assets import AssetKind
from central.media_queue import MEDIA_QUEUE, ProcrastinateMediaQueue
from central.media_repository import MediaRepository
from central.node_bus_wiring import NodeBus, build_node_bus
from central.photo_library.model import ConnectionSecret
from central.photo_library.ports import LibraryWorkerStore
from central.registry import RegistryError
from central.wall_plan.ports import KeyContentWriter
from contracts.models import Model, Positive
from contracts.time import Clock, SystemClock
from media.immich import ImmichClient, LibraryLimits
from media.key_content import MeasureError
from media.key_content import measure as measure_key_content
from media.library_checks import LibraryChecks
from media.library_client import LibraryClient, LibraryUnavailable, measure_readable_copy
from media.library_clients import LibraryClients
from media.library_thumbnails import LibraryThumbnailOrigin
from media.media_maker import MediaMaker, PreparationClient
from media.models import MediaError
from media.prepare import PreparationLimits, Preparer
from media.task_queue import (
    ProcrastinateCheckQueue,
    RetryableMediaTask,
    create_worker_app,
)

logger = logging.getLogger("photo_wall.worker")


# The job runtime's process-ending conditions; any other RuntimeError stays `worker_internal`.
_RUNTIME_EXITS: Final = frozenset({WORKER_EXITED, COMPLETION_NOT_RECORDED})


class WorkerLimits(Model):
    job_seconds: Positive = Field(default=330, le=330)
    close_seconds: Positive = Field(default=5, le=10)
    # Roadmap 5a: one key content measurement, Immich's two answers and the saliency included.
    measure_seconds: Positive = Field(default=30, le=120)


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
    def __init__(self, repository: MediaRepository,
                 library: LibraryWorkerStore, *,
                 preparer: PreparationClient | None = None,
                 client_factory: Callable[[ConnectionSecret], LibraryClient] | None = None,
                 limits: WorkerLimits = WorkerLimits(), previews: Path | None = None,
                 key_content: KeyContentWriter | None = None,
                 media_cache: MediaCacheBudget | None = None):
        self.repository, self.clock, self.limits = repository, repository.clock, limits
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
        # Roadmap 5a (slice M1): where measurements are read and recorded; `_entry` passes the
        # PostgreSQL store, and `measure` refuses with media_config without one.
        self.key_content = key_content
        # Follow-up S5: the media cache's one budget, kept on every maintenance pass.
        self.media_cache = media_cache
        self._error: str | None = None

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

    async def measure(self, asset_id: str, *, final_attempt: bool = False) -> None:
        """Measure one photo's key content (roadmap 5a, slice M1; MEASURE_KEY_CONTENT_TASK): read
        its asset row (gone: nothing to do), ask its connection's client for `faces` and the
        `thumbnail` at its revision, `media.key_content.measure` them off the event loop, and
        record the result. A decode failure, or the whole step running past
        `WorkerLimits.measure_seconds`, is recorded as failed (its retry is the store's); a
        library or asset-changed failure raises RetryableMediaTask (Procrastinate retries).

        Any other bounded thumbnail failure: one whose status is `unavailable` (Immich busy or
        not ready) is retried the same way; one that is not (an oversize or unsupported
        thumbnail) cannot be measured at this revision and is recorded as `decode`.

        On the `final_attempt` (the queue retries no more) a failure that would be retried is
        recorded as `timeout` instead, so the photo leaves the Planner's measurement requests
        until its retry and the photos ranked after it are measured (a photo Immich never
        answers for must not hold the top of the list)."""
        if self.key_content is None:
            raise MediaError("media_config", "incompatible")
        asset = await _blocking(self.key_content.asset_to_measure, asset_id)
        if asset is None:
            return
        try:
            async with asyncio.timeout(self.limits.measure_seconds):
                client = await self._client(asset.connection_ref)
                # Follow-up S1: the copy the readable size needs (a RAW photo's preview).
                known = await _blocking(self.library.copy_sizes, asset_id)
                measured = await measure_readable_copy(client, asset, known)
                await _blocking(self.library.record_copies, asset_id, measured)
                boxes = await client.faces(asset.upstream_id)
                thumbnail = await client.thumbnail(asset.upstream_id, asset.revision)
                key = await _blocking(measure_key_content, boxes, thumbnail)
        except MeasureError as error:
            await _blocking(self.key_content.record_failed, asset_id, error.reason)
            return
        except TimeoutError:
            await _blocking(self.key_content.record_failed, asset_id, "timeout")
            return
        except LibraryUnavailable as error:
            retryable = error.problem.kind
        except MediaError as error:
            if error.code != "asset_changed" and error.status != "unavailable":
                await _blocking(self.key_content.record_failed, asset_id, "decode")
                return
            retryable = error.code
        else:
            await _blocking(self.key_content.record_measured, asset_id, key)
            return
        if final_attempt:
            await _blocking(self.key_content.record_failed, asset_id, "timeout")
            return
        raise RetryableMediaTask(retryable) from None

    def media_maker(self, *, store: CacheStore, transactions: PgTransactions) -> MediaMaker:
        """The `PrepareMedia` handler over this worker's library, clients and preparer."""
        return MediaMaker(store=store, records=self.repository.records,
                          transactions=transactions, inputs=self.repository.variant_inputs,
                          library=self.library, client=self._clients.for_secret,
                          preparer=self.preparer, job_seconds=self.limits.job_seconds)

    async def maintain(self) -> None:
        """The thumbnail sweep, the guaranteed check-in, then the media cache's budget
        (`MediaCacheBudget`: pinned variants kept, the others least recently served first)."""
        # Listed BEFORE the purge: a file written after it is never discarded by this pass.
        files = await _blocking(self._thumbnail_files)
        recorded = await _blocking(self.library.retire_unwanted)
        await _blocking(self._discard_thumbnails, files, recorded)
        await _blocking(self.repository.worker_status, self._error)
        if self.media_cache is not None:
            removed = await _blocking(self.media_cache.enforce)
            if removed:
                logger.info("media cache: removed %d file(s)", len(removed))

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


async def _media_queue(app, additional_context: dict) -> None:
    """The legacy media queue itself (procrastinate, `MEDIA_QUEUE` only)."""
    await app.run_worker_async(
        queues=[MEDIA_QUEUE],
        concurrency=4,
        additional_context=additional_context,
        install_signal_handlers=False,
    )


async def _media_loop(worker: MediaWorker, run_queue: Callable[[], Awaitable[None]]) -> None:
    """The library checks' and measurements' queue, after one boot pass of maintenance (the
    thumbnail sweep, the guaranteed check-in, `WORKER_CHECK_IN_SECONDS`, and the media cache's
    budget). Media variants are
    the job runtime's (`PrepareMedia`), so no process waits for a media writer lock."""
    await worker.maintain()
    await run_queue()


async def _run_workers(media: Awaitable[None], runtime: JobRuntime,
                       bus: NodeBus | None, link: HomeAssistantLink) -> None:
    """The legacy media loop, the job runtime, the Home Assistant link and, with a hub deployed, the
    Node bus side by side, under ONE signal handler.

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
            group.create_task(until_stopped(link.run(bus_stop), stopped), name="worker home assistant link")

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


def build_bus_and_link(dsn: str, env: Mapping[str, str]) -> tuple[NodeBus | None, HomeAssistantLink]:
    """The worker's Node bus (None without a hub) and its Home Assistant link. The link has a database
    pool of its own and closes it when it ends; a recorded change wakes the bus's room-lights writer
    through the store's NOTIFY."""
    bus = build_node_bus(dsn, env)
    db = Database(dsn, pool_size=2)
    link = build_home_assistant_link(env, PgRoomLights(db), PgLinkHealth(db), close=db.close)
    return bus, link


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
    clock = SystemClock()
    repository = build_repository(dsn, clock, queue=ProcrastinateMediaQueue(dsn))
    db = repository.db
    # Roadmap 4: connections, their keys and the photo pools live in Central's database; the
    # checks read Immich through ImmichClient.for_connection (run ledger slice M2).
    library = PgPhotoLibrary(db, times=DatabaseTransactionClock(),
                             queue=ProcrastinateMediaQueue(dsn))
    cache_root = cache_layout.cache_root()
    previews = ensure_previews_directory(cache_root)
    ensure_media_directory(cache_root)
    media_store = CacheStore(CacheLayout(cache_root))
    worker = MediaWorker(
        repository, library, previews=previews,
        key_content=PgKeyContent(db, times=DatabaseTransactionClock()),
        media_cache=MediaCacheBudget(
            store=media_store, use=PgMediaVariantUse(),
            transactions=PgTransactions(db), settings=CacheSettings.from_env(os.environ)))
    # The library half of the thumbnail fetch (R22): only this package holds the key.
    thumbnails = LibraryThumbnailOrigin(
        library.pool_thumbnail, library.connection_secret,
        lambda secret: ImmichClient.for_connection(secret, clock=clock))
    # The one worker kind (design §10.5): every asset handler behind JobRuntime, in EVERY worker
    # process; only the legacy media loop is single-writer. Its boot checks run here, so a wiring
    # mistake fails before any job is claimed.
    runtime = build_job_runtime(
        db, clock, cache_root=cache_root, env=os.environ, thumbnails=thumbnails,
        media_maker=worker.media_maker(store=media_store,
                                       transactions=PgTransactions(db)))
    await _blocking(repository.db.migrate)
    # Settings › House starts from this process's zone when Central has not seeded it yet: the
    # worker reads Runtime (the Planner's Program windows) in house time, as Central does.
    await _blocking(seed_house, db, DatabaseTransactionClock())
    await _blocking(ProcrastinateMediaQueue.apply_schema, dsn)
    app = create_worker_app(dsn)
    checks = LibraryChecks(
        library, lambda secret: ImmichClient.for_connection(secret, clock=clock), clock,
        queue=ProcrastinateCheckQueue(app))
    additional_context = {"media_worker": worker, "library_checks": checks}
    media = _media_loop(worker, lambda: _media_queue(app, additional_context))
    try:
        async with app.open_async():
            await _run_workers(media, runtime, *build_bus_and_link(dsn, os.environ))
    finally:
        await worker._close_clients()
        await checks.close()
        await thumbnails.aclose()


def ensure_media_directory(cache_root: Path) -> Path:
    """Where media variants live (`media/blobs`, `CacheLayout`), created 0700 as the previews."""
    media = CacheLayout(cache_root).directory(AssetKind.MEDIA_VARIANT)
    media.mkdir(mode=0o700, parents=True, exist_ok=True)
    return media


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
