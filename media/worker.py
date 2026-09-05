"""Central-only media worker; secrets and upstream facts never enter Player delivery."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import signal
import stat
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal, Protocol

from pydantic import ConfigDict, Field, model_validator

from central.catalog import CatalogSnapshot
from central.db import Database
from central.media_repository import JobLease, MediaRepository
from central.media_store import MediaStore
from central.registry import RegistryError
from contracts.models import Model, Positive
from contracts.time import SystemClock
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
    poll_seconds: Positive = Field(default=1, le=60)
    heartbeat_seconds: Positive = Field(default=10, le=30)
    maintenance_seconds: Positive = Field(default=300, le=3600)
    refresh_seconds: Positive = Field(default=65, le=65)
    job_seconds: Positive = Field(default=330, le=330)
    close_seconds: Positive = Field(default=5, le=10)


class WorkerReport(Model):
    cycles: int = 0
    refreshes: int = 0
    jobs_ready: int = 0
    jobs_failed: int = 0
    pressure: int = 0
    stopped: bool = False
    last_error: str | None = Field(default=None, pattern=r"^[a-z_]{1,64}$")


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
        self._counts = dict(cycles=0, refreshes=0, jobs_ready=0, jobs_failed=0, pressure=0)
        self._error: str | None = None
        self._recipe: str | None = None
        self._needs_recovery = False
        self._running = False

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

    async def _heartbeat(self):
        while True:
            try:
                self._utc()
                await _blocking(self.repository.worker_status, self._error)
            except Exception as error:
                self._error = self._code(error)
            await asyncio.sleep(self.limits.heartbeat_seconds)

    async def _refresh_once(self) -> bool:
        lease = await _blocking(self.repository.begin_refresh)
        if lease is None:
            return False
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
        await _blocking(self.repository.publish_refresh, lease, result)
        self._counts["refreshes"] += 1
        if result.snapshot.status != "ok":
            self._error = result.diagnostics[0].code if result.diagnostics else "source_unavailable"
        return True

    async def _refresh_loop(self):
        while True:
            await asyncio.sleep(self.limits.poll_seconds)
            try:
                await self._refresh_once()
            except Exception as error:
                self._error = self._code(error)

    async def _fail(self, lease: JobLease, code: str):
        delay = (5, 15, 60)[min(lease.attempt - 1, 2)]
        retry_at = None if code in _PERMANENT else self._utc() + delay
        try:
            await _blocking(self.store.fail, lease, code, retry_at=retry_at)
        except RegistryError as error:
            if error.code != "stale_job":
                self._error, self._needs_recovery = self._code(error), True
        except Exception as error:
            self._error, self._needs_recovery = self._code(error), True

    async def _claim(self) -> JobLease | None:
        async def claim():
            operation = asyncio.create_task(_blocking(self.repository.claim_job))
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                claimed = await _drain(operation)
                if claimed:
                    await _drain(asyncio.create_task(self._fail(claimed, "worker_cancelled")))
                raise

        lease = await claim()
        if lease is not None:
            return lease
        health = await _blocking(self.repository.health)
        if health["worker_error"] == "storage_pressure":
            reserve = self.repository.limits.max_original_bytes + max(
                self.repository.limits.max_image_bytes, self.repository.limits.max_video_bytes)
            await _blocking(self.store.collect, target_bytes=max(0, health["max_bytes"] - reserve))
            lease = await claim()
            if lease is None:
                self._counts["pressure"] += 1
                self._error = "storage_pressure"
        return lease

    async def _job(self, lease: JobLease):
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
            self._counts["jobs_ready"] += 1
            self._error = None
        except asyncio.CancelledError:
            self._error = "worker_cancelled"
            await _drain(asyncio.create_task(self._fail(lease, "worker_cancelled")))
            raise
        except Exception as error:
            self._error = self._code(error)
            self._counts["jobs_failed"] += 1
            await self._fail(lease, self._error)

    async def _close_clients(self):
        async def close(client):
            try:
                async with asyncio.timeout(self.limits.close_seconds):
                    await client.close()
            except Exception:
                self._error = "connection_close"
        await asyncio.gather(*(close(client) for client in self._clients.values()))
        self._clients.clear()

    async def run(self, *, max_cycles: int | None = None, stop: asyncio.Event | None = None) -> WorkerReport:
        if self._running or (max_cycles is not None and (type(max_cycles) is not int or not 1 <= max_cycles <= 1_000_000)):
            raise MediaError("worker_config", "incompatible")
        self._running = True
        self._recipe = None
        self._counts = dict(cycles=0, refreshes=0, jobs_ready=0, jobs_failed=0, pressure=0)
        self._error = None
        background: list[asyncio.Task] = []
        stopped = False
        try:
            with self.store.worker_lock():
                try:
                    if stop is not None:
                        current = asyncio.current_task()
                        async def watch_stop():
                            await stop.wait()
                            current.cancel()
                        background.append(asyncio.create_task(watch_stop()))
                    await self._register_recipe()
                    background.append(asyncio.create_task(self._heartbeat()))
                    await self._refresh_once()
                    background.append(asyncio.create_task(self._refresh_loop()))
                    next_maintenance = self.clock.monotonic() + self.limits.maintenance_seconds
                    while max_cycles is None or self._counts["cycles"] < max_cycles:
                        self._utc()
                        await self._register_recipe()
                        now = self.clock.monotonic()
                        if not math.isfinite(now):
                            raise MediaError("clock_invalid")
                        if self._needs_recovery or now >= next_maintenance:
                            recovery = await _blocking(self.store.recover)
                            self._needs_recovery = recovery.pending > 0
                            next_maintenance = now + self.limits.maintenance_seconds
                        lease = await self._claim()
                        if lease:
                            await self._job(lease)
                        self._counts["cycles"] += 1
                        self._utc()
                        await _blocking(self.repository.worker_status, self._error)
                        if max_cycles is None or self._counts["cycles"] < max_cycles:
                            await asyncio.sleep(self.limits.poll_seconds)
                except asyncio.CancelledError:
                    stopped = True
                    if stop is None or not stop.is_set():
                        raise
                finally:
                    for task in background:
                        task.cancel()
                    async def finish():
                        await asyncio.gather(*background, return_exceptions=True)
                        await self._close_clients()
                        try:
                            self._utc()
                            await _blocking(self.repository.worker_status, self._error)
                        except Exception:
                            pass
                    await _drain(asyncio.create_task(finish()))
        finally:
            self._running = False
        return WorkerReport(**self._counts, stopped=stopped, last_error=self._error)


async def _entry(cycles):
    try:
        dsn, root, connection_file = (os.environ[name] for name in
            ("PHOTO_WALL_DATABASE_URL", "PHOTO_WALL_MEDIA_ROOT", "PHOTO_WALL_CONNECTIONS_FILE"))
    except KeyError:
        raise MediaError("worker_config", "incompatible") from None
    clock = SystemClock()
    repository = MediaRepository(Database(dsn), clock)
    worker = MediaWorker(repository, MediaStore(repository, Path(root)), load_connections(Path(connection_file)))
    await _blocking(repository.db.migrate)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    report = await worker.run(max_cycles=cycles, stop=stop)
    print(report.model_dump_json())


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the central Photo Wall media worker")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true")
    group.add_argument("--cycles", type=int)
    arguments = parser.parse_args()
    try:
        asyncio.run(_entry(1 if arguments.once else arguments.cycles))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(json.dumps({"error": MediaWorker._code(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
