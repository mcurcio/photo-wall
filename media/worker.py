"""Central-only media worker; secrets and upstream facts never enter Player delivery."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import stat
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal, Protocol

from pydantic import ConfigDict, Field, model_validator

from central.app_release_queue import APP_RELEASE_QUEUE
from central.app_release_service import AppReleaseService
from central.catalog import CatalogSnapshot
from central.db import Database
from central.media_queue import MEDIA_QUEUE, ProcrastinateMediaQueue
from central.media_repository import JobLease, MediaRepository, RefreshLease
from central.media_store import MediaStore
from central.registry import RegistryError
from contracts.models import Model, Positive
from contracts.time import SystemClock
from media.app_release_tasks import register_app_release_tasks
from media.immich import ImmichClient
from media.models import (
    ConnectionConfig,
    Diagnostic,
    DownloadedOriginal,
    MediaError,
    MediaLimits,
    OriginalAsset,
    RefreshResult,
    SourceSpec,
)
from media.prepare import BuildIdentity, PreparationLimits, PreparedMedia, Preparer
from media.task_queue import MediaTaskFailed, RetryableMediaTask, create_worker_app

logger = logging.getLogger("photo_wall.worker")


def worker_queues(release_enabled: bool) -> list[str]:
    """The queues this worker consumes, given whether release sourcing is on.

    MEDIA_QUEUE is always consumed. A task deferred onto a queue absent here is
    never dispatched, so gating APP_RELEASE_QUEUE on `release_enabled` is exactly
    what keeps an unconfigured worker (PHOTO_WALL_APP_ROOT unset) from ever
    polling GitHub (0010 bead-3 opt-in wiring; the wiring test mutation-probes
    both the presence when enabled and the absence when disabled).
    """
    queues = [MEDIA_QUEUE]
    if release_enabled:
        queues.append(APP_RELEASE_QUEUE)
    return queues


_FILE_LIMIT = 1024**2
_PERMANENT = frozenset({
    "asset_integrity", "asset_oversize", "unsupported_media", "unsupported_color",
    "metadata_invalid", "metadata_mismatch", "preparation_limit", "preparation_invalid",
    "media_original_mismatch", "media_result_mismatch", "media_journal_invalid",
})


class _ConnectionFile(Model):
    model_config = ConfigDict(hide_input_in_errors=True)
    schema_version: Literal[1] = Field(alias="schema")
    connections: tuple[ConnectionConfig, ...] = Field(max_length=128)

    @model_validator(mode="after")
    def unique_connections(self):
        if len({c.connection_id for c in self.connections}) != len(self.connections):
            raise ValueError("duplicate connection")
        return self


def load_connections(path: Path) -> dict[str, ConnectionConfig]:
    """Read a small private file as data; report no path, key, or validation input."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def constants(_):
        raise ValueError

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_uid != os.geteuid() or not 0 < before.st_size <= _FILE_LIMIT):
                raise ValueError
            content = stream.read(_FILE_LIMIT + 1)
            after = os.fstat(stream.fileno())
            if (len(content) != before.st_size or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns or before.st_mode != after.st_mode
                    or before.st_uid != after.st_uid):
                raise ValueError
        document = _ConnectionFile.model_validate(json.loads(content, object_pairs_hook=pairs, parse_constant=constants))
        return {connection.connection_id: connection for connection in document.connections}
    except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
        raise MediaError("connection_file", "incompatible") from None


class WorkerLimits(Model):
    refresh_seconds: Positive = Field(default=65, le=65)
    job_seconds: Positive = Field(default=330, le=330)
    close_seconds: Positive = Field(default=5, le=10)


class SourceClient(Protocol):
    async def refresh(self, spec: SourceSpec) -> RefreshResult: ...
    async def download_original(self, asset: OriginalAsset, destination: Path) -> DownloadedOriginal: ...
    async def close(self) -> None: ...


class PreparationClient(Protocol):
    async def describe_recipe(self) -> tuple[str, BuildIdentity]: ...
    async def prepare(self, asset: OriginalAsset, original: Path, destination: Path) -> PreparedMedia: ...


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
                 connections: Mapping[str, ConnectionConfig], *,
                 preparer: PreparationClient | None = None,
                 client_factory: Callable[[ConnectionConfig], SourceClient] | None = None,
                 limits: WorkerLimits = WorkerLimits()):
        self.repository, self.store, self.clock, self.limits = repository, store, repository.clock, limits
        if store.repository is not repository or limits.job_seconds + 30 > repository.limits.lease_seconds:
            raise MediaError("worker_config", "incompatible")
        if len(connections) > 128 or any(not isinstance(c, ConnectionConfig) or key != c.connection_id
                                         for key, c in connections.items()):
            raise MediaError("connection_config", "incompatible")
        self.connections = dict(connections)
        self.preparer = preparer or Preparer(clock=self.clock, limits=PreparationLimits(
            max_original_bytes=repository.limits.max_original_bytes,
            max_image_bytes=repository.limits.max_image_bytes,
            max_video_bytes=repository.limits.max_video_bytes,
            max_staging_bytes=repository.limits.max_original_bytes + max(
                repository.limits.max_image_bytes, repository.limits.max_video_bytes),
        ))
        self.client_factory = client_factory or (lambda config: ImmichClient(config, clock=self.clock,
            limits=MediaLimits(max_original_bytes=repository.limits.max_original_bytes)))
        self._clients: dict[str, SourceClient] = {}
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
        return "worker_internal"

    def _utc(self) -> float:
        now = self.clock.utc()
        if not math.isfinite(now):
            raise MediaError("clock_invalid")
        return now

    def _client(self, connection_id: str) -> SourceClient:
        if connection_id not in self.connections:
            raise MediaError("connection_unknown")
        if connection_id not in self._clients:
            self._clients[connection_id] = self.client_factory(self.connections[connection_id])
        return self._clients[connection_id]

    async def _register_recipe(self):
        self._utc()
        recipe, _ = await self.preparer.describe_recipe()
        if recipe != self._recipe:
            await _blocking(self.repository.set_recipe, recipe)
            self._recipe = recipe

    async def _refresh(self, lease: RefreshLease) -> None:
        try:
            async with asyncio.timeout(self.limits.refresh_seconds):
                result = await self._client(lease.source.connection_ref).refresh(lease.source)
        except asyncio.CancelledError:
            # No adapter task remains active when this handler is entered.
            result = RefreshResult(snapshot=CatalogSnapshot(source_ref=lease.source.source_ref,
                refreshed_at=self._utc(), status="unavailable"), diagnostics=(Diagnostic(code="worker_cancelled"),))
            task = asyncio.create_task(_blocking(self.repository.publish_refresh, lease, result))
            await _drain(task)
            raise
        except Exception as error:
            code = self._code(error)
            status = error.status if isinstance(error, MediaError) else "unavailable"
            result = RefreshResult(snapshot=CatalogSnapshot(source_ref=lease.source.source_ref,
                refreshed_at=self._utc(), status=status), diagnostics=(Diagnostic(code=code),))
        if not await _blocking(self.repository.publish_refresh, lease, result):
            raise RetryableMediaTask("stale_refresh")
        if result.snapshot.status == "ok":
            self._error = None
        else:
            self._error = result.diagnostics[0].code if result.diagnostics else "source_unavailable"
        await _blocking(self.repository.worker_status, self._error)

    async def refresh_once(self) -> bool:
        lease = await _blocking(self.repository.begin_scheduled_refresh)
        if lease is None:
            return False
        await self._refresh(lease)
        requested, completed = await _blocking(
            self.repository.refresh_revisions, lease.source.source_ref
        )
        if completed < requested:
            await self._refresh_source(lease.source.source_ref, retry_busy=False)
        return True

    async def refresh_source(self, source_ref: str) -> None:
        """Complete every persisted request revision for one source."""
        await self._refresh_source(source_ref, retry_busy=True)

    async def _refresh_source(self, source_ref: str, *, retry_busy: bool) -> None:
        for _ in range(8):
            lease = await _blocking(
                self.repository.begin_requested_refresh, source_ref
            )
            if lease is None:
                requested, completed = await _blocking(
                    self.repository.refresh_revisions, source_ref
                )
                if completed >= requested:
                    return
                if retry_busy:
                    raise RetryableMediaTask("refresh_in_progress")
                return
            await self._refresh(lease)
            requested, completed = await _blocking(
                self.repository.refresh_revisions, source_ref
            )
            if completed >= requested:
                return
        raise RetryableMediaTask("refresh_not_caught_up")

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
                client = self._client(lease.asset.connection_id)
                original = await client.download_original(lease.asset, paths.original)
                if original.path.absolute() != paths.original or original.sha1 != lease.asset.original_sha1:
                    raise MediaError("asset_integrity")
                prepared = await self.preparer.prepare(lease.asset, paths.original, paths.variant)
                if prepared.original_sha256 != original.sha256:
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
        await _blocking(self.repository.worker_status, self._error)

    async def _close_clients(self):
        async def close(client):
            try:
                async with asyncio.timeout(self.limits.close_seconds):
                    await client.close()
            except Exception:
                self._error = "connection_close"
        await asyncio.gather(*(close(client) for client in self._clients.values()))
        self._clients.clear()

async def _entry():
    try:
        dsn, root, connection_file = (os.environ[name] for name in
            ("PHOTO_WALL_DATABASE_URL", "PHOTO_WALL_MEDIA_ROOT", "PHOTO_WALL_CONNECTIONS_FILE"))
    except KeyError:
        raise MediaError("worker_config", "incompatible") from None
    clock = SystemClock()
    db = Database(dsn)
    queue = ProcrastinateMediaQueue(dsn)
    repository = MediaRepository(db, clock, queue=queue)
    worker = MediaWorker(repository, MediaStore(repository, Path(root)), load_connections(Path(connection_file)))
    # Release sourcing (0010) is OPT-IN: enabled only when PHOTO_WALL_APP_ROOT is
    # set (the shared storage the mirror lands bytes into, read by the serving
    # route). from_env returns None when unset, so an unconfigured worker
    # (compose, software-e2e) starts unchanged -- no service, no tasks, no queue,
    # no GitHub polling. When present it shares the media worker's DB pool.
    release_service = AppReleaseService.from_env(db, clock)
    await _blocking(repository.db.migrate)
    await _blocking(ProcrastinateMediaQueue.apply_schema, dsn)
    app = create_worker_app(dsn)
    additional_context = {"media_worker": worker}
    if release_service is not None:
        register_app_release_tasks(app)
        additional_context["app_release_service"] = release_service
    else:
        logger.info("release sourcing disabled: PHOTO_WALL_APP_ROOT unset")
    try:
        with worker.store.worker_lock():
            await worker._register_recipe()
            await worker.maintain()
            await worker.refresh_once()
            async with app.open_async():
                await app.run_worker_async(
                    queues=worker_queues(release_service is not None),
                    concurrency=4,
                    additional_context=additional_context,
                )
    finally:
        await worker._close_clients()


def main() -> int:
    try:
        asyncio.run(_entry())
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(json.dumps({"error": MediaWorker._code(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
