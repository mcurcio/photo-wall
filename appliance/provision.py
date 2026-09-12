"""0009 slice 2: the minimal base image's boot-time provisioner ("the bootstrapper").

Runs before the Player app exists on a diskless (RAM-root) base. It:

1. discovers central by mDNS (bounded; retries with backoff -- a base with no
   central on the LAN keeps trying, it never crashes or fabricates an origin);
2. fetches the app manifest (`GET /v1/app/manifest` -> `{version, sha256,
   size}`); a 503 `app_unconfigured` (no app promoted yet) is also retried;
3. downloads the `.deb` (`GET /v1/app/package/{sha256}.deb`, bounded/streamed)
   and verifies the downloaded bytes' sha256 against the manifest -- a
   **corruption check only** (0009 owner ruling: home LAN, no threat model,
   no signature anywhere). A mismatch discards the bytes and retries; it is
   never installed or run;
4. installs the `.deb` (unpack: `dpkg -i`) into the running RAM root;
5. hands the resolved origin forward as the app's explicit `central_origin`
   (`/etc/photo-wall/public.json`, the file `player.service.load_config`
   reads) so the Player app's own `resolve_origin` -- which consults
   discovery only when `central_origin is None`
   ([player/service.py:419](../player/service.py) -- skips a second,
   independently-resolved mDNS browse and enrolls against the exact central
   that served its code (see "the hard part" in
   docs/decisions/0009-minimal-base-and-app-package.md);
6. starts `photo-wall-player.service`.

It never enrolls -- the app enrolls by serial, once, after it starts
(0009 gate #2; a bootstrapper that also enrolled would double-enroll).

Reuse and the import-boundary decision
---------------------------------------
- Discovery: `player.mdns_discovery.MdnsCentralDiscovery`, the exact class the
  running app uses for the same LAN (the design's explicit call to share one
  discovery implementation rather than growing a second). `appliance`
  importing `player.mdns_discovery` crosses no import-linter contract: the
  two contracts in `pyproject.toml` forbid `contracts -> {central, media,
  player, appliance, ...}` and `player -> {central, appliance, ...}`; neither
  restricts `appliance -> player`. This module deliberately imports only
  `player.mdns_discovery`, never `player.service` (which pulls in GTK/
  GStreamer) -- the same "no GTK import at bootstrap time" split the design
  calls for.
- HTTP fetch: the *discipline* of `appliance/bootstrap.py`'s `Fetcher`
  (bounded deadline, no proxies/redirects, exact size bounds, streamed
  verification) is reproduced here as `AppFetcher` rather than importing
  `Fetcher` directly. `Fetcher` is constructed from a `BootConfig` that
  requires a baked HTTPS `release_origin` plus a pinned `boot_abi`
  (`appliance/bootstrap.py:92-125`); neither applies to an mDNS-*discovered*
  origin, which may legitimately be plain HTTP (T0 baseline,
  `player/mdns_discovery.py`) and carries no OS-ABI to pin. `AppFetcher`
  keeps the same guarantees (bounded deadline, no redirects, exact
  `Content-Length` bounds, streamed reads) without the initramfs-only
  `BootConfig` shape.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

LOG = logging.getLogger("photo_wall.appliance.provision")

CHUNK = 64 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
# Mirrors central.app_packages.MAX_APP_PACKAGE_BYTES by value, not by import:
# the base bootstrapper must not depend on `central` (FastAPI/psycopg), so the
# bound is restated here rather than shared.
MAX_APP_PACKAGE_BYTES = 1024**3
_SHA256 = re.compile(r"[0-9a-f]{64}")
DEFAULT_UNIT = "photo-wall-player.service"
DEFAULT_PUBLIC_CONFIG = Path("/etc/photo-wall/public.json")


class ProvisionError(ValueError):
    """A fixed diagnostic code; no untrusted command or HTTP output."""


class AppUnconfigured(ProvisionError):
    """Central has no app promoted yet (503 `app_unconfigured`) -- retry."""

    def __init__(self):
        super().__init__("app_unconfigured")


def _json(data: bytes) -> dict:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ProvisionError("provision_configuration")
            result[key] = value
        return result

    try:
        result = json.loads(
            data, object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProvisionError("provision_configuration") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProvisionError("provision_redirect")


class AppFetcher:
    """Bounded, no-redirect, deadline-guarded HTTP fetch for the app manifest
    and `.deb`, against an mDNS-discovered origin (http or https).

    Same discipline as `appliance/bootstrap.py`'s `Fetcher`: one deadline for
    the whole acquisition, no proxies, no redirects, an exact `Content-Length`
    bound enforced while streaming, `Content-Encoding` pinned to identity.
    """

    def __init__(self, origin: str, *, seconds: float = 60, opener=None,
                 monotonic=time.monotonic):
        if not 0 < seconds <= 300:
            raise ProvisionError("provision_deadline")
        self.origin = origin.rstrip("/")
        self.monotonic = monotonic
        self.deadline = monotonic() + seconds
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def chunks(self, path: str, maximum: int):
        if type(maximum) is not int or not 0 < maximum <= MAX_APP_PACKAGE_BYTES:
            raise ProvisionError("provision_limit")
        remaining = self.deadline - self.monotonic()
        if remaining <= 0:
            raise ProvisionError("provision_deadline")
        request = urllib.request.Request(
            self.origin + path, headers={"Accept-Encoding": "identity"}
        )
        total = 0
        try:
            with self.opener.open(request, timeout=min(10, remaining)) as response:
                if response.status == 503:
                    raise AppUnconfigured()
                if response.status != 200:
                    raise ProvisionError("provision_http")
                length = response.headers.get("Content-Length")
                if length is not None and (
                    not re.fullmatch(r"[0-9]{1,12}", length) or not 0 < int(length) <= maximum
                ):
                    raise ProvisionError("provision_limit")
                if response.headers.get("Content-Encoding", "identity") != "identity":
                    raise ProvisionError("provision_encoding")
                while block := response.read(CHUNK):
                    total += len(block)
                    if total > maximum:
                        raise ProvisionError("provision_limit")
                    if self.monotonic() >= self.deadline:
                        raise ProvisionError("provision_deadline")
                    yield block
                if not total or (length is not None and total != int(length)):
                    raise ProvisionError("provision_truncated")
        except (ProvisionError, AppUnconfigured):
            raise
        except urllib.error.HTTPError as error:
            if error.code == 503:
                raise AppUnconfigured() from None
            raise ProvisionError("provision_http") from None
        except (OSError, urllib.error.URLError, ValueError):
            raise ProvisionError("provision_network") from None

    def get(self, path: str, maximum: int) -> bytes:
        return b"".join(self.chunks(path, maximum))


def fetch_manifest(origin: str, *, seconds: float = 30, opener=None) -> dict:
    """`GET /v1/app/manifest` -> `{version, sha256, size}`; raises
    `AppUnconfigured` on a 503 (no app promoted -- caller retries)."""
    fetcher = AppFetcher(origin, seconds=seconds, opener=opener)
    manifest = _json(fetcher.get("/v1/app/manifest", MAX_MANIFEST_BYTES))
    if (
        set(manifest) != {"version", "sha256", "size"}
        or not isinstance(manifest["version"], str)
        or not 0 < len(manifest["version"]) <= 256
        or not isinstance(manifest["sha256"], str)
        or not _SHA256.fullmatch(manifest["sha256"])
        or type(manifest["size"]) is not int
        or not 0 < manifest["size"] <= MAX_APP_PACKAGE_BYTES
    ):
        raise ProvisionError("provision_manifest_invalid")
    return manifest


def fetch_package(origin: str, manifest: dict, *, seconds: float = 120, opener=None) -> bytes:
    """`GET /v1/app/package/{sha256}.deb`, bounded to the manifest's declared size."""
    fetcher = AppFetcher(origin, seconds=seconds, opener=opener)
    return fetcher.get(f"/v1/app/package/{manifest['sha256']}.deb", manifest["size"])


def dpkg_install(package: bytes, manifest: dict) -> None:
    """Default install step: unpack the `.deb` into the running RAM root via
    `dpkg -i`. Thin and replaceable -- tests inject a stub instead of
    shelling to real `dpkg` (this codebase has no dpkg-backed CI sandbox;
    real installation is the owner's Pi bench step)."""
    with tempfile.NamedTemporaryFile(suffix=".deb", delete=False) as handle:
        handle.write(package)
        deb_path = Path(handle.name)
    try:
        subprocess.run(["dpkg", "-i", str(deb_path)], check=True)
    finally:
        deb_path.unlink(missing_ok=True)


def write_public_config(origin: str, *, path: Path = DEFAULT_PUBLIC_CONFIG) -> None:
    """The origin handoff ("the hard part", 0009): write the bootstrapper's
    resolved central origin as the app's explicit `central_origin` in the
    file `player.service.load_config` reads
    (`/etc/photo-wall/public.json`, `appliance/systemd/player.service`'s
    `--config`). `PlayerConfig.central_origin`, once set, always wins over
    discovery -- `resolve_origin` consults `self.discovery` only when
    `central_origin is None` ([player/service.py:419](../player/service.py))
    -- so the app enrolls against the exact central that served its `.deb`,
    with no second, independently-resolved mDNS browse.

    A plain-HTTP discovered origin (the common home-LAN case, T0 baseline)
    needs `allow_http: true` alongside it: `PlayerConfig`'s explicit-origin
    validator requires that opt-in for an *explicit* HTTP origin even though
    a *discovered* one is accepted unconditionally
    (`player/service.py:_validate_origin`, `resolve_origin` passes
    `allow_http=True` for discovery but not for config). Without this, a
    home-LAN handoff of an `http://` origin would make the app's own
    `PlayerConfig` fail to load. Existing keys in `path` (if any) are
    preserved; only `schema`, `central_origin`, and (when needed)
    `allow_http` are set.
    """
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
            if not isinstance(existing, dict):
                existing = {}
        except (json.JSONDecodeError, OSError):
            existing = {}
    payload = {**existing, "schema": 1, "central_origin": origin}
    if urlsplit(origin).scheme == "http":
        payload["allow_http"] = True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True))


def start_player_unit(*, unit: str = DEFAULT_UNIT) -> None:
    """Thin, replaceable step: starts the Player systemd unit once the app is
    installed and the origin handoff is written. Tests inject a stub instead
    of shelling to real `systemctl`."""
    subprocess.run(["systemctl", "start", unit], check=True)


def _default_backoff(attempt: int) -> float:
    return min(2.0 * attempt, 30.0)


class Bootstrapper:
    """Orchestrates discover -> fetch manifest -> fetch + verify `.deb` ->
    install -> origin handoff -> start unit.

    Every external effect is an injected callable so the sequencing, the
    corruption guard, and the origin handoff can be unit tested with no real
    mDNS responder, HTTP server, dpkg, or systemd -- the actual Pi boot is
    the owner's bench step (see module docstring).

    `discovery` matches `player.discovery.CentralDiscovery`'s protocol
    (async `discover() -> str | None`) -- in production this is a
    `player.mdns_discovery.MdnsCentralDiscovery()`.
    """

    def __init__(
        self,
        *,
        discovery,
        fetch_manifest=fetch_manifest,
        fetch_package=fetch_package,
        install=dpkg_install,
        write_origin=write_public_config,
        start_unit=start_player_unit,
        sleep=asyncio.sleep,
        backoff=_default_backoff,
    ):
        self.discovery = discovery
        self.fetch_manifest = fetch_manifest
        self.fetch_package = fetch_package
        self.install = install
        self.write_origin = write_origin
        self.start_unit = start_unit
        self.sleep = sleep
        self.backoff = backoff

    async def run(self, *, max_attempts: int | None = None) -> bool:
        """Runs the provisioning state machine to completion.

        Returns True once the app is installed, the origin is handed off,
        and the unit is started. With `max_attempts=None` (production) it
        retries forever -- a base with no central, or a central with no app
        yet, must keep trying rather than crash or give up. Tests pass a
        bounded `max_attempts` and check the False return: bounded retry,
        no crash, no partial install.
        """
        attempt = 0
        while max_attempts is None or attempt < max_attempts:
            attempt += 1
            origin = await self.discovery.discover()
            if not origin:
                LOG.info("provision: no central discovered (attempt %d)", attempt)
                await self.sleep(self.backoff(attempt))
                continue
            try:
                manifest = self.fetch_manifest(origin)
            except AppUnconfigured:
                LOG.info("provision: app_unconfigured, retrying (attempt %d)", attempt)
                await self.sleep(self.backoff(attempt))
                continue
            except ProvisionError as error:
                LOG.warning("provision: manifest fetch failed: %s (attempt %d)", error, attempt)
                await self.sleep(self.backoff(attempt))
                continue
            try:
                package = self.fetch_package(origin, manifest)
            except ProvisionError as error:
                LOG.warning("provision: package fetch failed: %s (attempt %d)", error, attempt)
                await self.sleep(self.backoff(attempt))
                continue
            if hashlib.sha256(package).hexdigest() != manifest["sha256"]:
                # Corruption guard (0009 owner ruling: integrity, not
                # authenticity). Never install or start on a mismatch.
                LOG.warning("provision: app_integrity mismatch, discarding (attempt %d)", attempt)
                await self.sleep(self.backoff(attempt))
                continue
            self.install(package, manifest)
            self.write_origin(origin)
            self.start_unit()
            LOG.info("provision: app_installed %s", manifest["sha256"])
            return True
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Photo Wall base bootstrapper (0009)")
    parser.add_argument("--config", type=Path, default=DEFAULT_PUBLIC_CONFIG)
    parser.add_argument("--unit", default=DEFAULT_UNIT)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    from player.mdns_discovery import MdnsCentralDiscovery

    bootstrapper = Bootstrapper(
        discovery=MdnsCentralDiscovery(),
        write_origin=lambda origin: write_public_config(origin, path=args.config),
        start_unit=lambda: start_player_unit(unit=args.unit),
    )
    asyncio.run(bootstrapper.run())


if __name__ == "__main__":
    main()
