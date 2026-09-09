"""Bounded central transport for Immich v2.5.6, without storage or selection policy."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import math
import os
import re
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Self

import httpx

from central.catalog import CatalogSnapshot
from contracts.time import Clock, SystemClock
from media.models import (
    ConnectionConfig,
    Diagnostic,
    DownloadedOriginal,
    MediaError,
    MediaLimits,
    OriginalAsset,
    RefreshCounts,
    RefreshResult,
    SourceSpec,
    asset_identity,
    canonical_uuid,
)


@dataclass
class _Budget:
    clock: Clock
    started: float
    deadline: float
    json_bytes: int = 0
    search_requests: int = 0
    examined: int = 0
    duplicates: int = 0

    def remaining(self) -> float:
        now = self.clock.monotonic()
        if not math.isfinite(now) or now < self.started:
            raise MediaError("clock_invalid")
        if now >= self.deadline:
            raise MediaError("upstream_timeout")
        return self.deadline - now


@dataclass(frozen=True)
class _Head:
    upstream_id: str
    original_sha1: str
    kind: str
    captured_at: float


@dataclass(frozen=True)
class _Member:
    head: _Head
    asset: OriginalAsset | None = None
    problem: str | None = None


def _object(value: object) -> dict:
    if not isinstance(value, dict):
        raise MediaError("upstream_schema", "incompatible")
    return value


def _text(value: object, limit: int = 128) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        raise MediaError("upstream_schema", "incompatible")
    return value


def _uuid(value: object) -> str:
    try:
        return canonical_uuid(_text(value, 36))
    except ValueError:
        raise MediaError("upstream_schema", "incompatible") from None


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise MediaError("upstream_schema", "incompatible")
    return value


def _integer(value: object, *, code: str = "upstream_schema", minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise MediaError(code, "incompatible")
    return value


def _dimension(value: object, limit: int) -> int:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0 < value <= limit or not math.isfinite(value) or value != int(value)):
        raise MediaError("metadata_invalid", "incompatible")
    return int(value)


def _capture_time(value: object) -> float:
    try:
        parsed = datetime.fromisoformat(_text(value, 64).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        result = parsed.timestamp()
        if not math.isfinite(result):
            raise ValueError
        return result
    except (ValueError, OverflowError, OSError):
        raise MediaError("upstream_schema", "incompatible") from None


def _checksum(value: object) -> str:
    try:
        decoded = base64.b64decode(_text(value, 28), validate=True)
        if len(decoded) != 20:
            raise ValueError
        return decoded.hex()
    except (ValueError, binascii.Error):
        raise MediaError("upstream_schema", "incompatible") from None


def _no_constants(value: str) -> None:
    raise ValueError("nonfinite JSON")


class ImmichClient:
    """Own a nonredirecting client; inject transport and clock for portable fault tests.

    Operations are async so a slow HTTP stream is cancelled at the overall wall
    deadline, even when individual reads keep making progress. No background task
    or retry loop outlives one call. The worker owns retries and job leases.
    """

    def __init__(self, config: ConnectionConfig, *, limits: MediaLimits = MediaLimits(),
                 clock: Clock | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config, self.limits, self.clock = config, limits, clock or SystemClock()
        try:
            verify = ssl.create_default_context(cafile=config.ca_file) if config.ca_file else True
        except (OSError, ssl.SSLError):
            raise MediaError("connection_config", "incompatible") from None
        self._client = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/") + "/", trust_env=False,
            follow_redirects=False, verify=verify, transport=transport,
            headers={"x-api-key": config.api_key.get_secret_value(),
                     "Accept-Encoding": "identity"},
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    def _budget(self, seconds: float) -> _Budget:
        now = self.clock.monotonic()
        if not math.isfinite(now):
            raise MediaError("clock_invalid")
        return _Budget(self.clock, now, now + seconds)

    def _timeout(self, remaining: float) -> httpx.Timeout:
        return httpx.Timeout(
            connect=min(remaining, self.limits.connect_seconds),
            read=min(remaining, self.limits.read_seconds),
            write=min(remaining, self.limits.read_seconds),
            pool=min(remaining, self.limits.connect_seconds),
        )

    @staticmethod
    def _status(response: httpx.Response, *, original: bool = False) -> None:
        status = response.status_code
        if 300 <= status < 400:
            raise MediaError("upstream_redirect", "incompatible")
        if status in (401, 403):
            raise MediaError("asset_permission" if original else "upstream_permission", "permission")
        if original and status == 400:
            # This tag deliberately uses BadRequest for both an absent asset and
            # failed per-asset access. Do not infer which condition occurred.
            raise MediaError("asset_unavailable")
        if original and status in (404, 410):
            raise MediaError("asset_missing")
        if status == 429 or status >= 500:
            raise MediaError("upstream_unavailable")
        if status != 200:
            raise MediaError("upstream_schema", "incompatible")

    @staticmethod
    def _length(response: httpx.Response) -> int | None:
        encoding = response.headers.get("content-encoding", "identity").lower().strip()
        if encoding != "identity":
            raise MediaError("upstream_encoding", "incompatible")
        value = response.headers.get("content-length")
        if value is None:
            return None
        if not re.fullmatch(r"[0-9]{1,20}", value):
            raise MediaError("upstream_schema", "incompatible")
        return int(value)

    async def _json(self, method: str, path: str, budget: _Budget, *, body: dict | None = None,
                    original: bool = False) -> dict:
        remaining = min(budget.remaining(), self.limits.metadata_seconds)
        request_deadline = self.clock.monotonic() + remaining
        try:
            async with asyncio.timeout(remaining):
                async with self._client.stream(
                    method, path, json=body, timeout=self._timeout(remaining),
                    follow_redirects=False,
                ) as response:
                    budget.remaining()
                    self._status(response, original=original)
                    length = self._length(response)
                    if length is not None and length > self.limits.max_json_bytes:
                        raise MediaError("source_limit", "incompatible")
                    content_type = response.headers.get("content-type", "").split(";")[0].strip()
                    if content_type.lower() != "application/json":
                        raise MediaError("upstream_schema", "incompatible")
                    data = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        budget.remaining()
                        if self.clock.monotonic() >= request_deadline:
                            raise MediaError("upstream_timeout")
                        budget.json_bytes += len(chunk)
                        if (len(data) + len(chunk) > self.limits.max_json_bytes
                                or budget.json_bytes > self.limits.max_refresh_bytes):
                            raise MediaError("source_limit", "incompatible")
                        data.extend(chunk)
                    budget.remaining()
                    if self.clock.monotonic() >= request_deadline:
                        raise MediaError("upstream_timeout")
                    if length is not None and len(data) != length:
                        raise MediaError("upstream_integrity", "incompatible")
                    try:
                        return _object(json.loads(data, parse_constant=_no_constants))
                    except (ValueError, RecursionError, UnicodeError):
                        raise MediaError("upstream_schema", "incompatible") from None
        except (TimeoutError, httpx.TimeoutException):
            raise MediaError("upstream_timeout") from None
        except httpx.HTTPError:
            raise MediaError("upstream_unavailable") from None

    async def _check(self, budget: _Budget) -> None:
        version = await self._json("GET", "server/version", budget)
        if tuple(_integer(version.get(key)) for key in ("major", "minor", "patch")) != (2, 5, 6):
            raise MediaError("unsupported_version", "incompatible")
        user = await self._json("GET", "users/me", budget)
        if _uuid(user.get("id")) != self.config.owner_id:
            raise MediaError("owner_mismatch", "permission")

    async def check_connection(self) -> None:
        """Validate exact upstream version and configured API-key owner."""
        await self._check(self._budget(2 * self.limits.metadata_seconds))

    def _head(self, raw: object, spec: SourceSpec | None = None) -> _Head | None:
        row = _object(raw)
        upstream_id, owner = _uuid(row.get("id")), _uuid(row.get("ownerId"))
        kind = _text(row.get("type"))
        if kind not in ("IMAGE", "VIDEO", "AUDIO", "OTHER"):
            raise MediaError("upstream_schema", "incompatible")
        visibility = _text(row.get("visibility"))
        if visibility not in ("timeline", "archive", "hidden", "locked"):
            raise MediaError("upstream_schema", "incompatible")
        favorite = _boolean(row.get("isFavorite"))
        trashed, offline = _boolean(row.get("isTrashed")), _boolean(row.get("isOffline"))
        captured = _capture_time(row.get("fileCreatedAt"))
        sha1 = _checksum(row.get("checksum"))
        if (owner != self.config.owner_id or kind not in ("IMAGE", "VIDEO")
                or visibility != "timeline" or trashed or offline):
            return None
        kind = "image" if kind == "IMAGE" else "video"
        if spec is not None and (
            kind not in spec.media_types
            or (spec.favorites is not None and favorite != spec.favorites)
            or (spec.captured_from is not None and captured < spec.captured_from)
            or (spec.captured_until is not None and captured >= spec.captured_until)
        ):
            return None
        return _Head(upstream_id, sha1, kind, captured)

    def _original(self, raw: dict, head: _Head) -> OriginalAsset:
        exif = raw.get("exifInfo")
        if not isinstance(exif, dict):
            raise MediaError("metadata_invalid", "incompatible")
        width = _dimension(exif.get("exifImageWidth"), self.limits.max_dimension)
        height = _dimension(exif.get("exifImageHeight"), self.limits.max_dimension)
        if width * height > self.limits.max_pixels:
            raise MediaError("metadata_invalid", "incompatible")
        orientation = exif.get("orientation")
        if orientation is not None and orientation not in tuple(str(i) for i in range(1, 9)):
            raise MediaError("metadata_invalid", "incompatible")
        size = exif.get("fileSizeInByte")
        if size is not None:
            size = _integer(size, code="metadata_invalid", minimum=1)
            if size > self.limits.max_original_bytes:
                raise MediaError("asset_oversize", "incompatible")
        duration = None
        if head.kind == "video":
            value = raw.get("duration")
            if (not isinstance(value, str) or len(value) > 32
                    or not re.fullmatch(r"[0-9]{1,4}:[0-5][0-9]:[0-5][0-9](\.[0-9]{1,9})?", value)):
                raise MediaError("metadata_invalid", "incompatible")
            hours, minutes, seconds = value.split(":")
            duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            if not 0 < duration <= self.limits.max_video_seconds:
                raise MediaError("metadata_invalid", "incompatible")
        return OriginalAsset(
            connection_id=self.config.connection_id, upstream_id=head.upstream_id,
            original_sha1=head.original_sha1, kind=head.kind, captured_at=head.captured_at,
            raw_width=width, raw_height=height, orientation=int(orientation or "1"),
            file_size=size, duration=duration,
        )

    async def _walk(self, spec: SourceSpec, kind: str, with_exif: bool,
                    budget: _Budget) -> dict[str, _Member]:
        ceiling = min(self.limits.max_candidates + 1, 1000)
        body = dict(size=min(self.limits.page_size, ceiling), order="desc", type=kind.upper(),
                    visibility="timeline", isOffline=False, withDeleted=False, withExif=with_exif)
        if spec.favorites is not None:
            body["isFavorite"] = spec.favorites
        for field, value in (("takenAfter", spec.captured_from), ("takenBefore", spec.captured_until)):
            if value is not None:
                body[field] = datetime.fromtimestamp(value, timezone.utc).isoformat()
        while True:
            budget.remaining()
            if budget.search_requests >= self.limits.max_search_requests:
                raise MediaError("source_limit", "incompatible")
            budget.search_requests += 1
            # v2.5.6 has no stable secondary ordering for equal capture times.
            # Offset continuation can permanently miss rows even at rest. A
            # complete first page is the only accepted membership observation.
            payload = await self._json("POST", "search/metadata", budget, body={**body, "page": 1})
            assets = _object(payload.get("assets"))
            items = assets.get("items")
            if not isinstance(items, list):
                raise MediaError("upstream_schema", "incompatible")
            count = _integer(assets.get("count"))
            _integer(assets.get("total"))  # Tagged implementation returns a page count here.
            if count != len(items) or count > body["size"]:
                raise MediaError("upstream_pagination", "incompatible")
            if "nextPage" not in assets:
                raise MediaError("upstream_pagination", "incompatible")
            next_page = assets["nextPage"]
            if next_page is not None and (next_page != "2" or not items):
                raise MediaError("upstream_pagination", "incompatible")
            budget.examined += count
            if budget.examined > self.limits.max_examined_rows:
                raise MediaError("source_limit", "incompatible")
            if next_page is not None:
                if body["size"] >= ceiling:
                    raise MediaError("source_limit", "incompatible")
                body["size"] = ceiling
                continue
            members = {}
            seen = set()
            for raw in items:
                identifier = _uuid(_object(raw).get("id"))
                if identifier in seen:
                    budget.duplicates += 1
                    raise MediaError("upstream_pagination", "incompatible")
                seen.add(identifier)
                head = self._head(raw, spec)
                if head is None or head.kind != kind:
                    continue
                asset, problem = None, None
                if with_exif:
                    try:
                        asset = self._original(raw, head)
                    except MediaError as error:
                        problem = error.code
                members[head.upstream_id] = _Member(head, asset, problem)
                if len(members) > self.limits.max_candidates:
                    raise MediaError("source_limit", "incompatible")
            return members

    async def refresh(self, spec: SourceSpec) -> RefreshResult:
        """Produce one bounded observed membership; never substitute failure for empty."""
        diagnostics: list[Diagnostic] = []
        counts = dict(discovered=0, valid=0, pending=0, rejected=0)
        observed_at = self.clock.utc()
        if not math.isfinite(observed_at):
            # A finite observation instant is required to construct any snapshot.
            raise MediaError("clock_invalid")
        budget: _Budget | None = None
        # Frozen definitions can be admitted independently of refreshed membership.
        try:
            budget = self._budget(self.limits.refresh_seconds)
            if spec.connection_ref != self.config.connection_id:
                raise MediaError("connection_mismatch", "incompatible")
            async with asyncio.timeout(self.limits.refresh_seconds):
                await self._check(budget)
                walks: list[dict[str, _Member]] = []
                for with_exif in (False, True):
                    members: dict[str, _Member] = {}
                    for kind in spec.media_types:
                        members.update(await self._walk(spec, kind, with_exif, budget))
                        if len(members) > self.limits.max_candidates:
                            raise MediaError("source_limit", "incompatible")
                    walks.append(members)
                discovered, metadata = walks
                counts["discovered"] = len(discovered)
                originals: list[OriginalAsset] = []
                for identifier, member in discovered.items():
                    detail = metadata.get(identifier)
                    code = None
                    if detail is None or detail.head != member.head:
                        counts["pending"] += 1
                        code = "metadata_pending_or_changed"
                    elif detail.asset is None:
                        counts["rejected"] += 1
                        code = detail.problem or "metadata_invalid"
                    else:
                        originals.append(detail.asset)
                    if code and len(diagnostics) < self.limits.max_diagnostics - 1:
                        diagnostics.append(Diagnostic(code=code, asset_id=asset_identity(
                            self.config.connection_id, identifier, member.head.original_sha1,
                        )))
                budget.remaining()
                originals.sort(key=lambda asset: (-asset.captured_at, asset.asset_id))
                counts["valid"] = len(originals)
                status = "ok"
                if discovered and not originals:
                    status = "incompatible"
                    diagnostics.append(Diagnostic(code="metadata_pending_or_invalid"))
                refreshed_at = self.clock.utc()
                if not math.isfinite(refreshed_at):
                    raise MediaError("clock_invalid")
                return RefreshResult(
                    snapshot=CatalogSnapshot(source_ref=spec.source_ref, refreshed_at=refreshed_at,
                                             status=status,
                                             candidates=tuple(asset.candidate for asset in originals)),
                    assets=tuple(originals), diagnostics=tuple(diagnostics),
                    counts=RefreshCounts(**counts, duplicates=budget.duplicates,
                                         examined=budget.examined, search_requests=budget.search_requests,
                                         json_bytes=budget.json_bytes),
                )
        except TimeoutError:
            error = MediaError("upstream_timeout")
        except MediaError as caught:
            error = caught
        return RefreshResult(
            snapshot=CatalogSnapshot(source_ref=spec.source_ref, refreshed_at=observed_at,
                                     status=error.status),
            diagnostics=(Diagnostic(code=error.code),),
            counts=RefreshCounts(**{**counts, "valid": 0},
                                 duplicates=budget.duplicates if budget else 0,
                                 examined=budget.examined if budget else 0,
                                 search_requests=budget.search_requests if budget else 0,
                                 json_bytes=budget.json_bytes if budget else 0),
        )

    async def download_original(self, asset: OriginalAsset, destination: Path) -> DownloadedOriginal:
        """Recheck revision, then exclusively create/verify a caller-owned staging file.

        An existing path (including a symlink) is never followed or replaced. On a
        failed download only a file this operation created can be removed.
        """
        destination = Path(destination)
        budget = self._budget(self.limits.original_seconds)
        created = False
        try:
            async with asyncio.timeout(self.limits.original_seconds):
                if asset.connection_id != self.config.connection_id:
                    raise MediaError("connection_mismatch", "incompatible")
                await self._check(budget)
                raw = await self._json("GET", f"assets/{asset.upstream_id}", budget, original=True)
                head = self._head(raw)
                if head is None:
                    raise MediaError("asset_missing")
                fresh = self._original(raw, head)
                if fresh != asset:
                    raise MediaError("asset_changed")
                remaining = budget.remaining()
                async with self._client.stream(
                    "GET", f"assets/{asset.upstream_id}/original", params={"edited": "false"},
                    follow_redirects=False, timeout=self._timeout(remaining),
                ) as response:
                    budget.remaining()
                    self._status(response, original=True)
                    expected = self._length(response)
                    if expected is not None and expected > self.limits.max_original_bytes:
                        raise MediaError("asset_oversize", "incompatible")
                    if (expected is not None and asset.file_size is not None
                            and expected != asset.file_size):
                        raise MediaError("asset_integrity")
                    try:
                        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    except FileExistsError:
                        raise MediaError("destination_exists", "incompatible") from None
                    created = True
                    sha1, sha256, size = hashlib.sha1(), hashlib.sha256(), 0
                    with os.fdopen(fd, "wb") as output:
                        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                            budget.remaining()
                            size += len(chunk)
                            if size > self.limits.max_original_bytes:
                                raise MediaError("asset_oversize", "incompatible")
                            if ((expected is not None and size > expected)
                                    or (asset.file_size is not None and size > asset.file_size)):
                                raise MediaError("asset_integrity")
                            output.write(chunk)
                            sha1.update(chunk)
                            sha256.update(chunk)
                        budget.remaining()
                        if (size == 0 or (expected is not None and size != expected)
                                or (asset.file_size is not None and size != asset.file_size)
                                or sha1.hexdigest() != asset.original_sha1):
                            raise MediaError("asset_integrity")
                        output.flush()
                        os.fsync(output.fileno())
                    budget.remaining()
                    return DownloadedOriginal(path=destination, size=size,
                                              sha1=sha1.hexdigest(), sha256=sha256.hexdigest())
        except (TimeoutError, httpx.TimeoutException):
            raise MediaError("upstream_timeout") from None
        except httpx.HTTPError:
            raise MediaError("upstream_unavailable") from None
        except OSError:
            raise MediaError("staging_io") from None
        finally:
            # Return succeeded only after a complete verified file exists. Detect
            # exceptional exits without keeping an untrusted partial on disk.
            if created and sys.exc_info()[0] is not None:
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    # The owner must retain its reservation and reconcile this file.
                    raise MediaError("staging_cleanup") from None
