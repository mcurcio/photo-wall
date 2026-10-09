"""Unprivileged exact-environment preparation: no active pointer or effect port.

The download is one uplink `stream` (uplink's bounds: the status line may take STATUS_TIMEOUT,
Central's read-through wait plus one hop, contracts/read_through.py; each body read
READ_TIMEOUT), under an attempt deadline of its own. Given a `retry_until`, a transient failure
is retried within that window: never sooner than the reply's Retry-After (clamped to
[RETRY_AFTER_FLOOR_SECONDS, RETRY_AFTER_CEILING_SECONDS]) and never more than one attempt per
STATUS_TIMEOUT. Transient is: a timeout, a connection or name failure, a body cut short; a 502
or 504; a 503 that is not Central's (a proxy's, without Central's error body); and Central's
own 503 only when it carries a Retry-After (Central sends one only when the refusal is
transient: a cache miss outlived its read-through wait, the waiter slots are full). Any other
failure is terminal, Central's 503 without a Retry-After included. Without a window, a caller
retries on its own cadence (DesiredPreparation: the next poll)."""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from collections.abc import Callable, Iterable
from contextlib import closing
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from appliance.kernel.capacity import admit_preparation, memory_values
from contracts.app_environment import AppEnvironmentRefV2
from uplink.causes import Cause, UplinkError
from uplink.fetch import STATUS_TIMEOUT, Refused, stream
from uplink.origin import parse_url
from uplink.transport import HttpTransport, Transport
from uplink.trust import Trust

BLOCK: Final = 1024 * 1024
# The body after the status line: the largest artifact (the ~974 MB app environment) at
# 2.3 MiB/s (~19 Mbit/s), a link far below a Pi 5's gigabit port; a stalled body still ends
# after READ_TIMEOUT. The attempt's deadline starts before the request, so it absorbs the
# status wait: STATUS_TIMEOUT (35 s) + 420 s.
TRANSFER_SECONDS: Final = 420.0
ATTEMPT_SECONDS: Final = STATUS_TIMEOUT + TRANSFER_SECONDS
# Central's own Retry-After hints are 1-30 s (central/assets/reader.py); 1 s is floored.
RETRY_AFTER_FLOOR_SECONDS: Final = 5.0
RETRY_AFTER_CEILING_SECONDS: Final = 60.0
_TRANSIENT_TRANSFER: Final = frozenset({"deadline", "short", "tls"})
# A proxy in front of Central failing to reach it (502) or timing out (504).
_GATEWAY_STATUS: Final = frozenset({502, 504})
_FAULT_PREFIX: Final = "preparation_download_"


class DownloadFailed(ValueError):
    """One failed download attempt. args[0] is its boot-stage fault token; `retry_after` is None
    when the failure is terminal, else the least seconds to wait before another attempt."""

    def __init__(self, fault: str, retry_after: float | None = None) -> None:
        super().__init__(fault)
        self.retry_after = retry_after


class _NoAnchors:
    """An http origin needs no TLS anchors; HttpTransport reads `context` only for https."""

    @property
    def context(self):
        raise UplinkError(Cause.TLS, "trust_store", detail="http_only")


def _wait(retry_after: int | None) -> float:
    """A transient refusal's wait: its Retry-After clamped, the floor when it sent none."""
    return min(max(float(retry_after or 0), RETRY_AFTER_FLOOR_SECONDS), RETRY_AFTER_CEILING_SECONDS)


def _refused_wait(refused: Refused) -> float | None:
    """None (terminal) unless a gateway status, a 503 not written by Central, or Central's own
    503 with a Retry-After; Central sends a permanent 503 without one."""
    if refused.status in _GATEWAY_STATUS or (refused.status == 503 and (
            refused.cause is not Cause.CENTRAL or refused.retry_after is not None)):
        return _wait(refused.retry_after)
    return None


def _failure(error: UplinkError) -> DownloadFailed:
    if isinstance(error, Refused):
        fault = f"{_FAULT_PREFIX}refused:{error.status}"
        code = error.central_error
        if code is not None and len(fault) + 1 + len(code) <= 64:
            fault += ":" + code
        return DownloadFailed(fault, _refused_wait(error))
    transient = error.cause in (Cause.DNS, Cause.CONNECT) or (
        error.cause is Cause.TRANSFER and error.reason in _TRANSIENT_TRANSFER)
    return DownloadFailed(f"{_FAULT_PREFIX}{error.cause.value}_{error.reason}",
                          0.0 if transient else None)


class DownloadPreparer:
    def __init__(self, directory: Path, *, url: str, base_abi: str,
                 graphics_abi: str, plugin_abi: str, reserve_bytes: int = 256 * 1024**2,
                 claim=None, retry_until: float | None = None,
                 transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic):
        self.directory, self.url, self.reserve_bytes = directory, urlsplit(url), reserve_bytes
        self.claim, self.retry_until = claim, retry_until
        self.abi = dict(base_abi=base_abi, graphics_abi=graphics_abi, plugin_abi=plugin_abi)
        if self.url.scheme not in ("http", "https") or not self.url.hostname or self.url.username or self.url.password or self.url.fragment or self.url.query:
            raise ValueError("preparation_url_invalid")
        target = parse_url(url)
        if target is None:
            raise ValueError("preparation_url_invalid")
        self.target = target
        self._transport, self._sleep, self._monotonic = transport, sleep, monotonic

    def prepare(self, environment: AppEnvironmentRefV2) -> bool:
        if (environment.base_abi, environment.graphics_abi, environment.plugin_abi) != tuple(self.abi.values()):
            raise ValueError("preparation_abi_mismatch")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._clear_debris()
        disk = shutil.disk_usage(self.directory)
        total, available = memory_values()
        admit_preparation(environment.size_bytes, total=total, available=available, free=disk.free, used=disk.used)
        # Named by its digest alone: the stager (stage_image, or the online import worker)
        # re-hashes it by descriptor before anything uses it.
        download = self.directory / environment.environment_sha256
        if not download.exists():
            self._download(environment, download)
        return True

    def _clear_debris(self) -> None:
        """A killed attempt's `.partial` download, so that admission does not count it against
        the store. Complete downloads are digest-checked and stay (the caller owns them)."""
        for partial in self.directory.glob("*.partial"):
            partial.unlink(missing_ok=True)

    def _download(self, environment: AppEnvironmentRefV2, download: Path) -> None:
        """Attempts until one succeeds, a failure is terminal, or the next attempt could not get
        its status line before `retry_until`. Every attempt's deadline is within the window."""
        while True:
            started = self._monotonic()
            deadline = started + ATTEMPT_SECONDS
            if self.retry_until is not None:
                deadline = min(deadline, self.retry_until)
            try:
                self._attempt(environment, download, deadline)
                return
            except DownloadFailed as failure:
                if failure.retry_after is None or self.retry_until is None:
                    raise
                now = self._monotonic()
                wake = max(now + failure.retry_after, started + STATUS_TIMEOUT)
                if wake + STATUS_TIMEOUT > self.retry_until:
                    raise
                self._sleep(wake - now)

    def _transport_for(self) -> Transport:
        if self._transport is None:
            trust = Trust.public() if self.target.origin.scheme == "https" else _NoAnchors()
            self._transport = HttpTransport(trust=trust)  # type: ignore[arg-type]
        return self._transport

    def _attempt(self, environment: AppEnvironmentRefV2, download: Path, deadline: float) -> None:
        partial = self.directory / (environment.environment_sha256 + ".partial")
        partial.unlink(missing_ok=True)
        headers = {}
        if self.claim is not None:
            headers.update({"Authorization": "Bearer " + self.claim.credential, "X-Node-Session": str(self.claim.session_id)})
        try:
            try:
                body = stream(self._transport_for(), self.target, environment.size_bytes,
                              block=BLOCK, deadline=deadline, headers=headers,
                              monotonic=self._monotonic)
                with closing(body):
                    self._write(body, environment, partial)
            except UplinkError as error:
                raise _failure(error) from error
            os.replace(partial, download)
        finally:
            partial.unlink(missing_ok=True)

    @staticmethod
    def _write(body: Iterable[bytes], environment: AppEnvironmentRefV2, partial: Path) -> None:
        """`body` is bounded by the stream at the environment's size and is never empty or
        short of its declared length; the digest decides the rest."""
        total, hasher = 0, hashlib.sha256()
        with partial.open("xb") as output:
            for chunk in body:
                total += len(chunk)
                output.write(chunk)
                hasher.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        if total != environment.size_bytes or hasher.hexdigest() != environment.environment_sha256:
            raise DownloadFailed("preparation_digest_mismatch")
