"""GitHub Releases source client for the Player `.deb` (0010, bead 2).

Pure IO with an injectable transport: this module talks to the GitHub Releases
API, identifies each release's Player `.deb` via its `manifest.json` asset, and
streams the `.deb` down. It holds NO database, worker, or API wiring -- the
poller/mirror tasks (later beads) own the `AppReleases` store, the queue, and
the config/env. A `GithubReleaseSource` is constructed with a repo slug and an
optional `httpx.AsyncBaseTransport`, so every test runs against a fake transport
with no network (mirroring `media.immich.ImmichClient`).

Discipline reused (not reinvented) from `media.immich.ImmichClient` and
`appliance.provision.AppFetcher`: own a client whose transport is injectable,
pin `Accept-Encoding: identity`, refuse a non-identity `Content-Encoding`, bound
every read while streaming, verify the exact `Content-Length` when present, and
fail closed leaving no partial file on disk. sha256 is a **corruption check
only** (0009 home-LAN ruling); there is no signature verification anywhere.

The download URL is NOT carried in `manifest.json`
(`scripts/package_release_artifacts.py` emits none): `asset_url` is obtained by
joining `manifest.player_deb.filename` -> the release's `assets[].name` and
taking that asset's `browser_download_url`. GitHub asset URLs 302-redirect to a
signed CDN host, so the client follows a bounded number of redirects on the byte
fetches (see the errata note on 0010's "no redirect off-host" phrasing).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from central.app_packages import MAX_APP_PACKAGE_BYTES
from central.app_releases import AppReleaseError, parse_semver

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
MANIFEST_ASSET_NAME = "manifest.json"

CHUNK = 64 * 1024
PER_PAGE = 100
MAX_PAGES = 20  # per_page * MAX_PAGES = up to 2000 releases scanned per poll
MAX_MANIFEST_BYTES = 64 * 1024
MAX_RELEASES_PAGE_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5

_SHA256 = re.compile(r"[0-9a-f]{64}")
_REPO = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")


class GithubReleaseError(Exception):
    """A fixed diagnostic code for a transport/HTTP failure the poll cannot skip.

    `rate_limited` carries an optional `retry_after` (seconds) so the caller can
    honour GitHub's `Retry-After` per 0010's rate-limit hygiene. Per-release,
    deterministic conditions (no manifest, bad schema, unmatched asset) are NOT
    errors -- they are reported as an undeployable `DiscoveredRelease`, so one
    bad release never crashes the poll.
    """

    def __init__(self, code: str, *, retry_after: int | None = None):
        self.code = code
        self.retry_after = retry_after
        super().__init__(code)


@dataclass(frozen=True)
class DiscoveredRelease:
    """One release's discovery facts, shaped to feed `AppReleases.upsert_discovered`.

    `deployable` is True only when a Player `.deb` asset was resolved end to end
    (manifest schema 1, a valid `player_deb` record, and a matching attached
    asset). When False, `reason` is a code and `asset_url` is None; `asset_sha256`
    / `asset_size` may still be populated (from the manifest) when the manifest
    was readable but its named `.deb` was not attached -- the store uses that to
    tell an undeployable release from a re-cut (`divergent`) one.
    """

    tag: str
    is_prerelease: bool
    asset_sha256: str | None
    asset_size: int | None
    asset_url: str | None
    deployable: bool
    reason: str | None


@dataclass(frozen=True)
class ReleaseDiscovery:
    """The result of a `list_releases` poll: the records, the new list ETag, and
    whether the server answered 304 (list unchanged -> nothing re-fetched)."""

    releases: list[DiscoveredRelease]
    etag: str | None
    unchanged: bool


@dataclass(frozen=True)
class DownloadResult:
    """A verified, fully written `.deb`: its path, computed sha256, and byte size."""

    path: Path
    sha256: str
    size: int


class GithubReleaseSource:
    """Owns a non-proxying `httpx.AsyncClient`; inject `transport` for offline tests.

    Async so a mirror/poll task in the worker never blocks the event loop on a
    slow stream (the same reason `ImmichClient` is async). No retry loop or
    background task outlives one call; the worker owns retries and job leases.
    """

    def __init__(
        self,
        repo: str,
        *,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        api_base: str = GITHUB_API_BASE,
        max_deb_bytes: int = MAX_APP_PACKAGE_BYTES,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not isinstance(repo, str) or not _REPO.fullmatch(repo):
            raise GithubReleaseError("invalid_repo")
        if not isinstance(max_deb_bytes, int) or not 0 < max_deb_bytes <= MAX_APP_PACKAGE_BYTES:
            raise GithubReleaseError("invalid_bound")
        self.repo = repo
        self.api_base = api_base.rstrip("/")
        self.max_deb_bytes = max_deb_bytes
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "Accept-Encoding": "identity",
        }
        if token:
            # httpx strips Authorization on a cross-host redirect (the CDN hop),
            # which is correct here: the token authenticates the API host only.
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            trust_env=False,
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            transport=transport,
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    async def __aenter__(self) -> GithubReleaseSource:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # -- discovery -----------------------------------------------------------

    async def list_releases(
        self, *, etag: str | None = None, include_prereleases: bool = False
    ) -> ReleaseDiscovery:
        """Poll the paginated releases list and resolve each candidate's `.deb`.

        Sends `If-None-Match: <etag>` on the first page; a 304 short-circuits the
        whole poll (list unchanged) and returns `unchanged=True` cheaply. Drafts
        are always skipped; GitHub `prerelease` releases are skipped unless
        `include_prereleases`. Non-semver tags are skipped (no record). Returns
        the discovery records plus the first page's fresh ETag. Raises
        `GithubReleaseError` (rate_limited / list_unavailable / manifest_unavailable)
        on a condition that must abort the poll, so no rows are written from a
        partial view.
        """
        records: list[DiscoveredRelease] = []
        new_etag: str | None = None
        for page in range(1, MAX_PAGES + 1):
            status, page_etag, entries = await self._get_list_page(page, etag)
            if page == 1 and status == 304:
                return ReleaseDiscovery(releases=[], etag=etag, unchanged=True)
            if page == 1:
                new_etag = page_etag
            for entry in entries:
                record = await self._resolve(entry, include_prereleases)
                if record is not None:
                    records.append(record)
            if len(entries) < PER_PAGE:
                break
        return ReleaseDiscovery(releases=records, etag=new_etag, unchanged=False)

    async def _get_list_page(
        self, page: int, etag: str | None
    ) -> tuple[int, str | None, list]:
        headers = {"If-None-Match": etag} if page == 1 and etag else {}
        try:
            async with self._client.stream(
                "GET",
                f"{self.api_base}/repos/{self.repo}/releases",
                params={"per_page": PER_PAGE, "page": page},
                headers=headers,
            ) as response:
                if response.status_code == 304:
                    return 304, None, []
                self._raise_rate_limit(response)
                if response.status_code != 200:
                    raise GithubReleaseError("list_unavailable")
                body = await self._read_body(response, MAX_RELEASES_PAGE_BYTES)
                page_etag = response.headers.get("ETag")
        except httpx.HTTPError:
            raise GithubReleaseError("list_unavailable") from None
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError):
            raise GithubReleaseError("list_invalid") from None
        if not isinstance(data, list):
            raise GithubReleaseError("list_invalid")
        return 200, page_etag, data

    async def _resolve(self, entry: object, include_prereleases: bool) -> DiscoveredRelease | None:
        if not isinstance(entry, dict) or entry.get("draft") is True:
            return None
        is_prerelease = bool(entry.get("prerelease"))
        if is_prerelease and not include_prereleases:
            return None
        tag = entry.get("tag_name")
        if not isinstance(tag, str):
            return None
        try:
            parse_semver(tag)
        except AppReleaseError:
            return None  # non-semver tag: skipped, no record (0010)

        assets: dict[str, str] = {}
        raw_assets = entry.get("assets")
        if isinstance(raw_assets, list):
            for asset in raw_assets:
                if isinstance(asset, dict):
                    name, url = asset.get("name"), asset.get("browser_download_url")
                    if isinstance(name, str) and isinstance(url, str):
                        assets.setdefault(name, url)

        def undeployable(reason: str, sha=None, size=None) -> DiscoveredRelease:
            return DiscoveredRelease(tag, is_prerelease, sha, size, None, False, reason)

        manifest_url = assets.get(MANIFEST_ASSET_NAME)
        if manifest_url is None:
            return undeployable("no_manifest")
        body = await self._fetch_manifest(manifest_url)
        if body is None:  # manifest asset listed but gone (404/410)
            return undeployable("no_manifest")
        try:
            manifest = json.loads(body)
        except (ValueError, UnicodeError):
            return undeployable("manifest_invalid")
        if not isinstance(manifest, dict):
            return undeployable("manifest_invalid")
        if manifest.get("schema") != 1:
            # Runtime schema guard: the design is pinned to schema 1 and refuses,
            # gracefully, anything else rather than crashing the poll (0010).
            return undeployable("schema_mismatch")
        player = self._player_deb(manifest)
        if player is None:
            return undeployable("manifest_invalid")
        filename, sha256, size = player
        asset_url = assets.get(filename)
        if asset_url is None:
            # Manifest names a `.deb` not attached to the release: undeployable,
            # and (for an already-mirrored tag) the store's `divergent` signal --
            # so the sha256/size are still reported.
            return undeployable("asset_missing", sha=sha256, size=size)
        return DiscoveredRelease(tag, is_prerelease, sha256, size, asset_url, True, None)

    async def _fetch_manifest(self, url: str) -> bytes | None:
        """Return the manifest bytes, or None when the asset is absent (404/410).

        Raises `manifest_unavailable` on a transient failure so the poll aborts
        and retries wholesale rather than poisoning a row as undeployable.
        """
        try:
            async with self._client.stream("GET", url) as response:
                if response.status_code in (404, 410):
                    return None
                if response.status_code != 200:
                    raise GithubReleaseError("manifest_unavailable")
                return await self._read_body(response, MAX_MANIFEST_BYTES)
        except httpx.HTTPError:
            raise GithubReleaseError("manifest_unavailable") from None

    @staticmethod
    def _player_deb(manifest: dict) -> tuple[str, str, int] | None:
        player = manifest.get("player_deb")
        if not isinstance(player, dict):
            return None
        filename, sha256, size = player.get("filename"), player.get("sha256"), player.get("size")
        if (
            not isinstance(filename, str)
            or not filename.endswith(".deb")
            or not isinstance(sha256, str)
            or not _SHA256.fullmatch(sha256)
            or type(size) is not int
            or not 0 < size <= MAX_APP_PACKAGE_BYTES
        ):
            return None
        return filename, sha256, size

    # -- download ------------------------------------------------------------

    async def download(
        self,
        asset_url: str,
        dest: Path,
        *,
        sha256: str | None = None,
        max_bytes: int | None = None,
    ) -> DownloadResult:
        """Stream a `.deb` to `dest`, bounded and hashed; fail closed on any fault.

        The running-total abort against `max_bytes` (default `MAX_APP_PACKAGE_BYTES`)
        is the ONLY bound on the downloaded bytes (0010: `register()` validates its
        size argument, not the on-disk bytes). `dest` is created exclusively
        (`O_EXCL`); on any failure -- oversize, truncation, bad encoding, or (when
        `sha256` is given) a corruption-only hash mismatch -- the partial file is
        removed and a `GithubReleaseError` is raised, so a corrupt download never
        leaves servable bytes. This is a clean seam for the mirror job (bead 3),
        which owns the `.deb.tmp` -> `app-<sha>.deb` rename and the registry write.
        """
        if not isinstance(asset_url, str) or not asset_url:
            raise GithubReleaseError("invalid_asset_url")
        maximum = self.max_deb_bytes if max_bytes is None else max_bytes
        if not isinstance(maximum, int) or not 0 < maximum <= self.max_deb_bytes:
            raise GithubReleaseError("invalid_bound")
        if sha256 is not None and (not isinstance(sha256, str) or not _SHA256.fullmatch(sha256)):
            raise GithubReleaseError("invalid_sha256")
        dest = Path(dest)
        digest = hashlib.sha256()
        size = 0
        created = False
        try:
            async with self._client.stream("GET", asset_url) as response:
                if response.status_code != 200:
                    raise GithubReleaseError("download_unavailable")
                self._check_identity(response, "download_encoding")
                length = self._content_length(response)
                if length is not None and length > maximum:
                    raise GithubReleaseError("download_too_large")
                try:
                    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    raise GithubReleaseError("destination_exists") from None
                created = True
                with os.fdopen(fd, "wb") as output:
                    async for chunk in response.aiter_bytes(CHUNK):
                        size += len(chunk)
                        if size > maximum:
                            raise GithubReleaseError("download_too_large")
                        output.write(chunk)
                        digest.update(chunk)
                    if size == 0 or (length is not None and size != length):
                        raise GithubReleaseError("download_truncated")
                    output.flush()
                    os.fsync(output.fileno())
        except httpx.HTTPError:
            raise GithubReleaseError("download_unavailable") from None
        except OSError:
            raise GithubReleaseError("download_io") from None
        finally:
            # A complete, verified file exists only on the success path; on any
            # exceptional exit remove the partial so nothing untrusted persists.
            if created and sys.exc_info()[0] is not None:
                dest.unlink(missing_ok=True)
        computed = digest.hexdigest()
        if sha256 is not None and computed != sha256:
            dest.unlink(missing_ok=True)  # corruption-only check (0009); never serve mangled bytes
            raise GithubReleaseError("download_corrupt")
        return DownloadResult(path=dest, sha256=computed, size=size)

    # -- streaming helpers ---------------------------------------------------

    async def _read_body(self, response: httpx.Response, maximum: int) -> bytes:
        self._check_identity(response, "response_encoding")
        length = self._content_length(response)
        if length is not None and length > maximum:
            raise GithubReleaseError("response_too_large")
        data = bytearray()
        async for chunk in response.aiter_bytes(CHUNK):
            if len(data) + len(chunk) > maximum:
                raise GithubReleaseError("response_too_large")
            data.extend(chunk)
        if length is not None and len(data) != length:
            raise GithubReleaseError("response_truncated")
        return bytes(data)

    @staticmethod
    def _content_length(response: httpx.Response) -> int | None:
        value = response.headers.get("content-length")
        if value is None or not re.fullmatch(r"[0-9]{1,20}", value.strip()):
            return None  # absent or malformed: the running-total bound still guards
        return int(value)

    @staticmethod
    def _check_identity(response: httpx.Response, code: str) -> None:
        encoding = response.headers.get("content-encoding", "identity").lower().strip()
        if encoding not in ("", "identity"):
            raise GithubReleaseError(code)

    def _raise_rate_limit(self, response: httpx.Response) -> None:
        if response.status_code in (403, 429):
            retry_after = None
            raw = response.headers.get("Retry-After")
            if raw is not None and re.fullmatch(r"[0-9]{1,6}", raw.strip()):
                retry_after = int(raw)
            raise GithubReleaseError("rate_limited", retry_after=retry_after)
