"""The GitHub Releases gateway: the kernel `ReleaseOrigin` for the Player `.deb` and the OS image.

A port of `central/github_releases.py` (which stays until P2 deletes it). The discipline is
unchanged: identity encoding pinned and enforced, every read bounded while streaming, an exact
`Content-Length` check, no partial file ever left behind, and a bounded number of redirects (GitHub
asset URLs 302 to a signed CDN host; httpx strips `Authorization` on that cross-host hop). What
changed is the shape (kernel `ReleaseListing` / `PublishedRelease` / `OriginLocator`) and the
failure classification: every failure is `OriginUnavailable` (retry) or `OriginRejected` (do not).

Each call opens and closes its own `httpx.AsyncClient`, so there is no lifecycle to manage.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO, Final

import httpx

from central.kernel.assets import OriginLocator
from central.kernel.handling import OriginRejected, OriginUnavailable
from central.kernel.ports import PublishedRelease, ReleaseListing
from central.kernel.types import release_version, require_sha256
from contracts.release import MAX_ROOTFS_BYTES

GITHUB_API_BASE: Final = "https://api.github.com"
MAX_DOWNLOAD_BYTES: Final = MAX_ROOTFS_BYTES  # 1024**3: the largest artifact Central serves

GITHUB_API_VERSION: Final = "2022-11-28"
MANIFEST_ASSET_NAME: Final = "manifest.json"
DEFAULT_REPO: Final = "mcurcio/photo-wall"

CHUNK: Final = 64 * 1024
PER_PAGE: Final = 100
MAX_PAGES: Final = 20  # PER_PAGE * MAX_PAGES = up to 2000 releases scanned per listing
MAX_MANIFEST_BYTES: Final = 64 * 1024
MAX_RELEASES_PAGE_BYTES: Final = 8 * 1024 * 1024
MAX_REDIRECTS: Final = 5

_REPO = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
_CONTENT_LENGTH = re.compile(r"[0-9]{1,20}")
_RETRY_AFTER = re.compile(r"[0-9]{1,6}")
_TRUTHY = frozenset({"1", "true", "yes", "on"})


class _BodyTooLarge(Exception):
    """A bounded read exceeded its bound (classified by the caller)."""


class _BodyTruncated(Exception):
    """A bounded read ended short of its declared `Content-Length` (classified by the caller)."""


class _BodyEncoded(Exception):
    """A response carried a non-identity `Content-Encoding` (classified by the caller)."""


@dataclass(frozen=True, slots=True)
class _Manifest:
    """What one release's `manifest.json` yields: the `.deb` (or why not) and the OS image."""

    package: OriginLocator | None
    package_problem: str | None
    os_image: OriginLocator | None


class GitHubReleaseOrigin:
    """Implements kernel `ReleaseOrigin` against the GitHub Releases API; inject `transport`
    for offline tests."""

    def __init__(self, repo: str, *, token: str | None = None, include_prereleases: bool = False,
                 transport: httpx.AsyncBaseTransport | None = None, api_base: str = GITHUB_API_BASE,
                 timeout: timedelta = timedelta(seconds=30)) -> None:
        if not isinstance(repo, str) or _REPO.fullmatch(repo) is None:
            raise ValueError("invalid_repo")
        if not isinstance(timeout, timedelta) or timeout <= timedelta(0):
            raise ValueError("invalid_timeout")
        self.repo = repo
        self.include_prereleases = bool(include_prereleases)
        self._api_base = api_base.rstrip("/")
        self._transport = transport
        self._timeout = timeout.total_seconds()
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "Accept-Encoding": "identity",
        }
        if token:
            # httpx strips Authorization on a cross-host redirect (the CDN hop), which is
            # correct: the token authenticates the API host only.
            self._headers["Authorization"] = f"Bearer {token}"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GitHubReleaseOrigin:
        """The rules of `AppReleaseService.from_env`: repo, optional token, prerelease opt-in."""
        env = os.environ if env is None else env
        return cls(
            env.get("PHOTO_WALL_RELEASE_REPO", DEFAULT_REPO),
            token=env.get("PHOTO_WALL_RELEASE_TOKEN") or None,
            include_prereleases=env.get("PHOTO_WALL_RELEASE_PRERELEASES", "").lower() in _TRUTHY,
        )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            trust_env=False,
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            transport=self._transport,
            headers=self._headers,
            timeout=httpx.Timeout(self._timeout),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    # -- listing -------------------------------------------------------------

    async def list_releases(self, *, etag: str | None) -> ReleaseListing:
        """Every published release, or `unchanged=True` on a page-1 304. Never a partial list:
        any failure that is not one release's own deterministic problem aborts the listing."""
        releases: list[PublishedRelease] = []
        new_etag: str | None = None
        async with self._client() as client:
            for page in range(1, MAX_PAGES + 1):
                unchanged, page_etag, entries = await self._list_page(client, page, etag)
                if unchanged:
                    return ReleaseListing(releases=(), etag=etag, unchanged=True)
                if page == 1:
                    new_etag = page_etag
                for entry in entries:
                    published = await self._resolve(client, entry)
                    if published is not None:
                        releases.append(published)
                if len(entries) < PER_PAGE:
                    break
        return ReleaseListing(releases=tuple(releases), etag=new_etag, unchanged=False)

    async def _list_page(self, client: httpx.AsyncClient, page: int,
                         etag: str | None) -> tuple[bool, str | None, list[object]]:
        headers = {"If-None-Match": etag} if page == 1 and etag else {}
        try:
            async with client.stream(
                "GET", f"{self._api_base}/repos/{self.repo}/releases",
                params={"per_page": PER_PAGE, "page": page}, headers=headers,
            ) as response:
                if page == 1 and response.status_code == 304:
                    return True, None, []
                _raise_for_status(response, rejected="list_rejected")
                body = await _read_body(response, MAX_RELEASES_PAGE_BYTES)
                page_etag = response.headers.get("ETag")
        except (httpx.HTTPError, _BodyTruncated):
            raise OriginUnavailable("origin_unreachable") from None
        except (_BodyTooLarge, _BodyEncoded):
            raise OriginRejected("list_invalid") from None
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError):
            raise OriginRejected("list_invalid") from None
        if not isinstance(data, list):
            raise OriginRejected("list_invalid")
        return False, page_etag, data

    async def _resolve(self, client: httpx.AsyncClient, entry: object) -> PublishedRelease | None:
        if not isinstance(entry, dict) or entry.get("draft") is True:
            return None
        is_prerelease = bool(entry.get("prerelease"))
        if is_prerelease and not self.include_prereleases:
            return None
        tag = entry.get("tag_name")
        try:
            release_version(tag)  # type: ignore[arg-type]
        except ValueError:
            return None  # non-semver tag: skipped, no record
        assets = _asset_urls(entry.get("assets"))
        manifest = await self._manifest(client, assets)
        return PublishedRelease(tag=tag, is_prerelease=is_prerelease, package=manifest.package,
                                package_problem=manifest.package_problem,
                                os_image=manifest.os_image)

    async def _manifest(self, client: httpx.AsyncClient, assets: dict[str, str]) -> _Manifest:
        manifest_url = assets.get(MANIFEST_ASSET_NAME)
        if manifest_url is None:
            return _Manifest(None, "no_manifest", None)
        try:
            body = await self._fetch_manifest(client, manifest_url)
        except (_BodyTooLarge, _BodyEncoded):
            return _Manifest(None, "manifest_invalid", None)  # deterministic: this release only
        if body is None:  # listed, but gone upstream (404/410)
            return _Manifest(None, "no_manifest", None)
        try:
            manifest = json.loads(body)
        except (ValueError, UnicodeError):
            return _Manifest(None, "manifest_invalid", None)
        if not isinstance(manifest, dict):
            return _Manifest(None, "manifest_invalid", None)
        if manifest.get("schema") != 1:
            return _Manifest(None, "schema_mismatch", None)
        # The OS image is independent of the .deb: parsed whatever the player_deb outcome.
        os_image = _locator(manifest.get("base_image"), assets, suffix="", max_size=None)
        player = manifest.get("player_deb")
        if _declared_file(player, suffix=".deb", max_size=MAX_DOWNLOAD_BYTES) is None:
            return _Manifest(None, "manifest_invalid", os_image)
        package = _locator(player, assets, suffix=".deb", max_size=MAX_DOWNLOAD_BYTES)
        if package is None:  # the manifest names a .deb the release does not attach
            return _Manifest(None, "asset_missing", os_image)
        return _Manifest(package, None, os_image)

    async def _fetch_manifest(self, client: httpx.AsyncClient, url: str) -> bytes | None:
        """The manifest bytes; None when absent (404/410). A transient failure aborts the whole
        listing (`manifest_unavailable`) rather than recording the release as broken."""
        try:
            async with client.stream("GET", url) as response:
                if response.status_code in (404, 410):
                    return None
                _raise_rate_limit(response)
                if response.status_code != 200:
                    raise OriginUnavailable("manifest_unavailable")
                return await _read_body(response, MAX_MANIFEST_BYTES)
        except (httpx.HTTPError, _BodyTruncated):
            raise OriginUnavailable("manifest_unavailable") from None

    # -- download ------------------------------------------------------------

    async def download(self, locator: OriginLocator, into: Path, *, max_bytes: int) -> None:
        """Stream `locator` into a new file `into` (O_EXCL, 0600), bounded, verified and fsynced.

        On any failure `into` is removed; a local `OSError` is re-raised as-is after the removal.
        """
        if not isinstance(locator, OriginLocator):
            raise ValueError("invalid_locator")
        if type(max_bytes) is not int or not 0 < max_bytes <= MAX_DOWNLOAD_BYTES:
            raise ValueError("invalid_max_bytes")
        into = Path(into)
        fd = os.open(into, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # FileExistsError as-is
        try:
            with os.fdopen(fd, "wb") as output:
                await self._stream_into(locator, output, max_bytes)
                output.flush()
                await _off_loop(os.fsync, output.fileno())
        except BaseException:
            into.unlink(missing_ok=True)
            raise

    async def _stream_into(self, locator: OriginLocator, output: BinaryIO, max_bytes: int) -> None:
        if locator.size is not None and locator.size > max_bytes:
            raise OriginRejected("download_too_large")
        digest = hashlib.sha256()
        size = 0
        try:
            async with self._client() as client, client.stream("GET", locator.url) as response:
                if response.status_code in (404, 410):
                    raise OriginRejected("download_not_found")
                _raise_for_status(response, rejected="download_rejected")
                if not _identity(response):
                    raise OriginRejected("download_encoding")
                length = _content_length(response)
                if length is not None and length > max_bytes:
                    raise OriginRejected("download_too_large")
                async for chunk in response.aiter_bytes(CHUNK):
                    size += len(chunk)
                    if size > max_bytes:
                        raise OriginRejected("download_too_large")
                    output.write(chunk)  # OSError as-is
                    digest.update(chunk)
        except httpx.HTTPError:
            raise OriginUnavailable("origin_unreachable") from None
        if size == 0 or (length is not None and size != length):
            raise OriginUnavailable("download_truncated")
        if locator.size is not None and size != locator.size:
            raise OriginUnavailable("download_corrupt")
        if locator.sha256 is not None and digest.hexdigest() != locator.sha256:
            raise OriginUnavailable("download_corrupt")


# -- parsing and streaming helpers ---------------------------------------------


async def _off_loop(function: Callable[..., object], *args: object) -> None:
    """Run blocking `function` on a worker thread; a cancellation still waits for it to end,
    so the caller's file descriptor outlives the call."""
    task = asyncio.ensure_future(asyncio.to_thread(function, *args))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({task})
        task.exception()  # retrieved: the cancellation is what propagates
        raise


def _asset_urls(raw_assets: object) -> dict[str, str]:
    assets: dict[str, str] = {}
    if isinstance(raw_assets, list):
        for asset in raw_assets:
            if isinstance(asset, dict):
                name, url = asset.get("name"), asset.get("browser_download_url")
                if isinstance(name, str) and isinstance(url, str):
                    assets.setdefault(name, url)
    return assets


def _declared_file(record: object, *, suffix: str,
                   max_size: int | None) -> tuple[str, str, int] | None:
    """(filename, sha256, size) of a manifest file record, or None when malformed."""
    if not isinstance(record, dict):
        return None
    filename, sha256, size = record.get("filename"), record.get("sha256"), record.get("size")
    if not isinstance(filename, str) or not filename.endswith(suffix):
        return None
    try:
        require_sha256(sha256)  # type: ignore[arg-type]
    except ValueError:
        return None
    if type(size) is not int or size <= 0 or (max_size is not None and size > max_size):
        return None
    return filename, sha256, size


def _locator(record: object, assets: dict[str, str], *, suffix: str,
             max_size: int | None) -> OriginLocator | None:
    """The download locator for a manifest file record: its filename joined to the release's
    attached assets (the manifest carries no URL). None when malformed or not attached."""
    declared = _declared_file(record, suffix=suffix, max_size=max_size)
    if declared is None:
        return None
    filename, sha256, size = declared
    url = assets.get(filename)
    if url is None:
        return None
    try:
        return OriginLocator(url=url, sha256=sha256, size=size)
    except ValueError:
        return None  # an unusable asset URL is the same as no asset


def _raise_rate_limit(response: httpx.Response) -> None:
    if response.status_code in (403, 429):
        retry_after = None
        raw = response.headers.get("Retry-After")
        if raw is not None and _RETRY_AFTER.fullmatch(raw.strip()):
            retry_after = timedelta(seconds=int(raw.strip()))
        raise OriginUnavailable("rate_limited", retry_after=retry_after)


def _raise_for_status(response: httpx.Response, *, rejected: str) -> None:
    """200 passes; 403/429 is `rate_limited`; any other 4xx is terminal; the rest is transient."""
    if response.status_code == 200:
        return
    _raise_rate_limit(response)
    if 400 <= response.status_code < 500:
        raise OriginRejected(rejected)
    raise OriginUnavailable("origin_error")


async def _read_body(response: httpx.Response, maximum: int) -> bytes:
    if not _identity(response):
        raise _BodyEncoded
    length = _content_length(response)
    if length is not None and length > maximum:
        raise _BodyTooLarge
    data = bytearray()
    async for chunk in response.aiter_bytes(CHUNK):
        if len(data) + len(chunk) > maximum:
            raise _BodyTooLarge
        data.extend(chunk)
    if length is not None and len(data) != length:
        raise _BodyTruncated
    return bytes(data)


def _content_length(response: httpx.Response) -> int | None:
    value = response.headers.get("content-length")
    if value is None or _CONTENT_LENGTH.fullmatch(value.strip()) is None:
        return None  # absent or malformed: the running-total bound still guards
    return int(value)


def _identity(response: httpx.Response) -> bool:
    return response.headers.get("content-encoding", "identity").lower().strip() in ("", "identity")
