"""0009 slice 2: the minimal base image's boot-time provisioner ("the bootstrapper").

Runs before the Player app exists on a diskless (RAM-root) base. Every attempt:

1. finds Central through `uplink.finder.find_central` (R1): the kernel command line's
   `photowall.central` root wins; only when the command line names no Central does it browse
   mDNS (`player.mdns_discovery`, handed the `Unconfigured` proof). The root is then located
   again (`uplink.locate`, the one request that follows redirects), so a gateway that moved
   Central since the last attempt is followed;
2. fetches the app manifest (`GET /v1/app/manifest` -> `{version, sha256, size}`) and the
   `.deb` (`GET /v1/app/package/{sha256}.deb`) through `uplink.fetch.DirectFetch`, only from
   the located origin, never through a redirect;
3. verifies the downloaded bytes' sha256 against the manifest -- a **corruption check only**
   (0009 owner ruling: home LAN, no threat model, no signature anywhere). A mismatch discards
   the bytes and retries; they are never installed or run;
4. installs the `.deb` with `dpkg --install` alone into the running RAM root. Nothing on the
   device resolves packages: the base installed the Player's `Depends` at image build, from
   the same declaration they are rendered from (`scripts/debian_packages.py`). A `Depends` the
   base lacks makes dpkg exit non-zero naming it, and the unit's start limit reboots the Pi;
5. writes the handoff, `/etc/photo-wall/public.json` (0644 in a 0755 directory, whatever the
   unit's umask): `central_origin` only when the root came from mDNS, so the Player finds the
   same Central without a second browse; a command-line root is read by the Player itself;
6. starts `photo-wall-player.service`.

It never enrolls -- the app enrolls by serial, once, after it starts (0009 gate #2). It never
steps the clock either: stage 1 did, once, and recorded it (rule 3). A `time` failure therefore
leaves the process (exit 1); the unit's start limit reboots the Pi, and stage 1 tries again.
Every other network failure is one named `uplink` cause, logged and retried.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from appliance.bootstrap import read_pi_serial
from contracts.clock_record import ClockRecord
from contracts.strict_json import loads_object
from uplink.causes import Cause, UplinkError
from uplink.clock import RunClockRecord
from uplink.diagnosis import failure_text
from uplink.fetch import DirectFetch
from uplink.files import write_atomically
from uplink.finder import CentralDiscovery, Found, find_central
from uplink.locate import LOCATE_DEADLINE
from uplink.origin import Origin
from uplink.resolver import (
    KERNEL_COMMAND_LINE,
    Unconfigured,
    read_kernel_command_line,
    resolve_central,
)
from uplink.transport import HttpTransport, Transport
from uplink.trust import Trust
from uplink.watchdog import extend_start
from uplink.watchdog import ready as watchdog_ready

LOG = logging.getLogger("photo_wall.appliance.provision")

CHUNK = 64 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
# The Player's own bound on public.json (player.service.MAX_JSON), restated by value: the
# bootstrapper does not import player.service (GTK, httpx, pydantic).
MAX_PUBLIC_CONFIG_BYTES = 1024 * 1024
# Mirrors central.app_packages.MAX_APP_PACKAGE_BYTES by value, not by import: the base
# bootstrapper must not depend on `central` (FastAPI/psycopg).
MAX_APP_PACKAGE_BYTES = 1024**3
_SHA256 = re.compile(r"[0-9a-f]{64}")
# Release tag shape, mirrors contracts.models.BaseHealth.running_tag by value: contracts.models
# needs pydantic, which the bootstrapper does not carry.
_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]*")
MAX_TAG = 256
MAX_VERSION = 256
# Pinned by value on both sides (central.netboot_base.SERIAL_HEADER,
# appliance.netboot_init.SERIAL_HEADER); the appliance sends its Pi serial so
# central resolves the per-device tag it was served this boot.
SERIAL_HEADER = "X-PhotoWall-Serial"
# Opt-in env gate for the per-device `.deb` path (0012 bead 6): unset -> the
# device keeps 0010's global `.deb` (GET /v1/app/manifest) and posts no
# base-health, so an appliance that has not opted in is unchanged.
PER_DEVICE_ENV = "PHOTO_WALL_PER_DEVICE_DEB"
APP_MANIFEST_PATH: Final = "/v1/app/manifest"
DEVICE_MANIFEST_PATH: Final = "/v1/netboot/manifest"
DEFAULT_UNIT = "photo-wall-player.service"
DEFAULT_PUBLIC_CONFIG: Final = Path("/etc/photo-wall/public.json")
MANIFEST_SECONDS: Final = 30.0
PACKAGE_SECONDS: Final = 120.0
# The mDNS browse (player.mdns_discovery), passed to it explicitly by main(). A command-line
# root never browses.
DISCOVERY_SECONDS: Final = 3.0
# `dpkg --install` (subprocess timeout): the Player .deb is a pure unpack plus a postinst that
# creates the `wall` user.
INSTALL_SECONDS: Final = 60.0
# `systemctl start` (subprocess timeout) waits for the Player's READY=1 (Type=notify).
# photo-wall-player.service sets no TimeoutStartSec, so systemd fails that start itself after its
# DefaultTimeoutStartSec, 90 s: waiting longer here would wait on a start systemd gave up on.
START_UNIT_SECONDS: Final = 90.0
MAX_BACKOFF_SECONDS: Final = 30.0
# An allowance, NOT an enforced bound: the CPU- and RAM-local steps -- the sha256 of up to
# MAX_APP_PACKAGE_BYTES, the 0600 temp .deb write and the handoff write.
LOCAL_STEPS_SECONDS: Final = 15.0
# M5: the one deadline owner after switch_root is the unit's TimeoutStartSec, which
# Bootstrapper.run() renews through uplink.watchdog.extend_start() as each attempt BEGINS. The
# longest wait between two renewals is therefore one whole attempt plus the backoff after it:
# the SUM of every blocking step, each at its bound. The window is that sum with a 1.25x margin,
# so a healthy attempt, however slow, is never killed, and a hung one is killed within it.
ATTEMPT_MARGIN: Final = 1.25
LONGEST_ATTEMPT_SECONDS: Final = (
    DISCOVERY_SECONDS           # find: the mDNS browse (Unconfigured only)
    + LOCATE_DEADLINE           # find: locate's whole redirect chain
    + MANIFEST_SECONDS          # DirectFetch: the whole manifest exchange
    + PACKAGE_SECONDS           # DirectFetch: the whole package exchange
    + LOCAL_STEPS_SECONDS       # sha256, temp .deb, handoff (allowance)
    + INSTALL_SECONDS           # dpkg --install
    + START_UNIT_SECONDS        # systemctl start
    + MAX_BACKOFF_SECONDS)      # the sleep before the next attempt's renewal
# Kept equal to TimeoutStartSec in appliance/systemd/photo-wall-provision.service by hand -- a
# unit file cannot import a Python constant (tests/test_netboot_liveness.py pins both).
PROVISION_ATTEMPT_TIMEOUT_SECONDS: Final = math.ceil(ATTEMPT_MARGIN * LONGEST_ATTEMPT_SECONDS)


class ProvisionError(ValueError):
    """A content failure with a fixed code (e.g. provision_manifest_invalid). Network failures
    are UplinkError, never this."""


@dataclass(frozen=True, slots=True)
class AppManifest:
    version: str            # 1..256 characters
    sha256: str             # 64 lower-case hex
    size: int               # 1..MAX_APP_PACKAGE_BYTES
    tag: str | None         # per-device path only; the release-tag shape


def parse_manifest(body: bytes, *, per_device: bool) -> AppManifest:
    """contracts.strict_json.loads_object(body, max_bytes=MAX_MANIFEST_BYTES); exactly the
    keys {version, sha256, size} (+ tag when per_device), each checked as today. Anything else
    raises ProvisionError("provision_manifest_invalid")."""
    document = loads_object(body, max_bytes=MAX_MANIFEST_BYTES)
    required = {"version", "sha256", "size"} | ({"tag"} if per_device else set())
    if document is None or set(document) != required:
        raise ProvisionError("provision_manifest_invalid")
    version, sha256, size = document["version"], document["sha256"], document["size"]
    tag = document.get("tag")
    if (not isinstance(version, str) or not 0 < len(version) <= MAX_VERSION
            or not isinstance(sha256, str) or not _SHA256.fullmatch(sha256)
            or type(size) is not int or not 0 < size <= MAX_APP_PACKAGE_BYTES
            or (per_device and (not isinstance(tag, str) or len(tag) > MAX_TAG
                                or not _TAG.fullmatch(tag)))):
        raise ProvisionError("provision_manifest_invalid")
    return AppManifest(version, sha256, size, tag)


def fetch_manifest(fetch: DirectFetch, serial: str | None) -> AppManifest:
    """serial None: GET /v1/app/manifest. serial set (opt-in PHOTO_WALL_PER_DEVICE_DEB):
    GET /v1/netboot/manifest with X-PhotoWall-Serial, so the `.deb` rides the exact tag whose
    base this device was served this boot (0012 bead 6). A 503 app_unconfigured arrives as
    UplinkError(CENTRAL, "error", central_error="app_unconfigured")."""
    if serial is None:
        return parse_manifest(fetch.get(APP_MANIFEST_PATH, MAX_MANIFEST_BYTES), per_device=False)
    body = fetch.get(DEVICE_MANIFEST_PATH, MAX_MANIFEST_BYTES, headers={SERIAL_HEADER: serial})
    return parse_manifest(body, per_device=True)


def fetch_package(fetch: DirectFetch, manifest: AppManifest) -> bytes:
    """GET /v1/app/package/{sha256}.deb, bounded to manifest.size."""
    return b"".join(fetch.chunks(f"/v1/app/package/{manifest.sha256}.deb", manifest.size,
                                 block=CHUNK))


def install_package(package: bytes, manifest: AppManifest) -> None:
    """Write the bytes to a 0600 temp .deb, then `dpkg --install <tmp>.deb` with
    DEBIAN_FRONTEND=noninteractive; nothing else. No apt, no package lists, no network: the
    Player's Depends are already on the base, because the base installed
    packages("bootstrapper", "player") from the same declaration the Player's Depends come
    from. A Depends the base lacks makes dpkg exit non-zero with its own message naming the
    package; that subprocess.CalledProcessError escapes (see Bootstrapper.run), as does the
    subprocess.TimeoutExpired of a dpkg that outlives INSTALL_SECONDS. dpkg's own output goes to
    the journal. The temp file is removed in every case. Tests inject a stub."""
    environment = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    # mkstemp creates the file 0600 whatever the umask; a failed write (a full RAM root) is
    # cleaned up like a failed install.
    handle = tempfile.NamedTemporaryFile(suffix=".deb", delete=False)
    deb_path = Path(handle.name)
    try:
        with handle:
            handle.write(package)
        subprocess.run(["dpkg", "--install", os.fspath(deb_path)], check=True, env=environment,
                       timeout=INSTALL_SECONDS)
    finally:
        deb_path.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class Handoff:
    discovered_root: Origin | None   # set only when the root came from mDNS
    base_running_tag: str | None     # per-device path only


def _existing_keys(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_PUBLIC_CONFIG_BYTES + 1)
    except OSError:
        return {}
    return loads_object(data, max_bytes=MAX_PUBLIC_CONFIG_BYTES) or {}


def write_handoff(handoff: Handoff, *, path: Path = DEFAULT_PUBLIC_CONFIG) -> None:
    """Keep any existing keys; set schema 1; set central_origin to the discovered root or remove
    it; remove allow_http; set or remove base_running_tag. Written with
    uplink.files.write_atomically(path, ..., mode=0o644), so the `wall` user can read it
    whatever the unit's umask."""
    payload = {**_existing_keys(path), "schema": 1}
    payload.pop("allow_http", None)
    for key, value in (("central_origin", handoff.discovered_root),
                       ("base_running_tag", handoff.base_running_tag)):
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = str(value)
    write_atomically(path, json.dumps(payload, sort_keys=True).encode(), mode=0o644)


def start_player_unit(*, unit: str = DEFAULT_UNIT) -> None:
    """Thin, replaceable step: starts the Player systemd unit once the app is
    installed and the handoff is written, waiting at most START_UNIT_SECONDS
    (subprocess.TimeoutExpired escapes, like a failed start). Tests inject a stub
    instead of shelling to real `systemctl`."""
    subprocess.run(["systemctl", "start", unit], check=True, timeout=START_UNIT_SECONDS)


def _default_backoff(attempt: int) -> float:
    return min(2.0 * attempt, MAX_BACKOFF_SECONDS)


class Bootstrapper:
    """Orchestrates find -> fetch manifest -> fetch + verify `.deb` -> install -> handoff ->
    start unit. Every external effect is injected, so the sequencing, the corruption guard and
    the handoff are unit tested with no real mDNS responder, Central, dpkg or systemd; the real
    Pi boot is the owner's bench step. `transport` is the one the located origin is fetched
    over (the same one `find` locates with, in production). `clock` is read only by
    failure_text."""

    def __init__(self, *, find: Callable[[], Awaitable[Found]], transport: Transport,
                 clock: ClockRecord | None,
                 fetch_manifest: Callable[[DirectFetch, str | None], AppManifest] = fetch_manifest,
                 fetch_package: Callable[[DirectFetch, AppManifest], bytes] = fetch_package,
                 install: Callable[[bytes, AppManifest], None] = install_package,
                 write_handoff: Callable[[Handoff], None] = write_handoff,
                 start_unit: Callable[[], None] = start_player_unit,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 backoff: Callable[[int], float] = _default_backoff,
                 serial_reader: Callable[[], str | None] | None = None,
                 watchdog_extend: Callable[[float], bool] = extend_start,
                 watchdog_ready: Callable[[], bool] = watchdog_ready) -> None:
        self._find, self._transport, self._clock = find, transport, clock
        self._fetch_manifest, self._fetch_package = fetch_manifest, fetch_package
        self._install, self._write_handoff, self._start_unit = install, write_handoff, start_unit
        self._sleep, self._backoff = sleep, backoff
        # `serial_reader` opts the device into the per-device `.deb` path (0012 bead 6): the
        # manifest fetch carries the serial and Central returns the served tag, handed
        # forward for base-health. Unset (default) keeps 0010's global `.deb`.
        self._serial_reader = serial_reader
        # M5: default to the real systemd-notify calls (no-ops, returning False, when not run
        # under systemd's Type=notify -- see uplink.watchdog). Tests inject fakes.
        self._watchdog_extend, self._watchdog_ready = watchdog_extend, watchdog_ready

    def _serial(self) -> str | None:
        if self._serial_reader is None:
            return None
        try:
            return self._serial_reader()
        except OSError:
            return None

    def _fetch(self, found: Found, seconds: float) -> DirectFetch:
        return DirectFetch(found.central, transport=self._transport, seconds=seconds)

    async def run(self, *, max_attempts: int | None = None) -> bool:
        """One attempt:
          1. found = await find()                         (locates again on every attempt)
          2. manifest = fetch_manifest(DirectFetch(found.central, seconds=30), serial)
          3. package = fetch_package(DirectFetch(found.central, seconds=120), manifest)
          4. sha256 mismatch -> log, back off, next attempt
          5. install(package, manifest)                   (dpkg only)
          6. write_handoff(Handoff(found.root if found.source == "discovered" else None, tag))
          7. start_unit(); return True
        An UplinkError is logged as failure_text(error, clock=clock) and retried after
        backoff, EXCEPT cause TIME, which is re-raised: nothing in stage 2 can fix the clock, so
        the process exits into the unit's start limit and the reboot path (rule 3).
        ProvisionError is retried. Install, handoff and start failures escape. With
        max_attempts=None (production) it retries forever; False after max_attempts.
        M5: watchdog_extend(PROVISION_ATTEMPT_TIMEOUT_SECONDS) fires once as every attempt
        begins -- the one renewal site, so no exit from an attempt (a retried failure, a
        discarded corrupt package, a future branch) can skip it, and process start-up never
        eats into attempt 1's window. Each window thus spans one attempt plus its backoff
        (LONGEST_ATTEMPT_SECONDS). watchdog_ready() fires exactly once, right after
        start_unit(), on the successful return (Type=notify's starting phase does not end
        without READY=1)."""
        attempt = 0
        while max_attempts is None or attempt < max_attempts:
            attempt += 1
            self._watchdog_extend(PROVISION_ATTEMPT_TIMEOUT_SECONDS)
            try:
                found = await self._find()
                LOG.info("provision: central %s (%s root %s)", found.central.origin,
                         found.source, found.root)
                manifest = self._fetch_manifest(self._fetch(found, MANIFEST_SECONDS),
                                                self._serial())
                package = self._fetch_package(self._fetch(found, PACKAGE_SECONDS), manifest)
            except UplinkError as error:
                if error.cause is Cause.TIME:
                    raise
                LOG.warning("provision: %s (attempt %d)",
                            failure_text(error, clock=self._clock), attempt)
                await self._sleep(self._backoff(attempt))
                continue
            except ProvisionError as error:
                LOG.warning("provision: %s (attempt %d)", error, attempt)
                await self._sleep(self._backoff(attempt))
                continue
            if hashlib.sha256(package).hexdigest() != manifest.sha256:
                # Corruption guard (0009 owner ruling: integrity, not authenticity). Never
                # install or start on a mismatch.
                LOG.warning("provision: app_integrity mismatch, discarding (attempt %d)", attempt)
                await self._sleep(self._backoff(attempt))
                continue
            self._install(package, manifest)
            self._write_handoff(Handoff(found.root if found.source == "discovered" else None,
                                        manifest.tag))
            self._start_unit()
            self._watchdog_ready()
            LOG.info("provision: app_installed %s", manifest.sha256)
            return True
        return False


def main(argv: Sequence[str] | None = None) -> None:
    """--config PATH, --unit NAME, --cmdline PATH (default /proc/cmdline; as stage 1).
    Once: resolution = resolve_central(read_kernel_command_line(cmdline)); trust =
    Trust.public(); transport = HttpTransport(trust=trust); clock = RunClockRecord().read()
    (read only, never stepped; used only by failure_text); discovery =
    MdnsCentralDiscovery(timeout=DISCOVERY_SECONDS) only when resolution is Unconfigured (lazy
    import: zeroconf is not needed when the cmdline
    names Central). Then asyncio.run(Bootstrapper(...).run()). An UplinkError that escapes is logged with
    failure_text and exits 1 (systemd restarts; 10 exits in 10 minutes reboot the Pi)."""
    parser = argparse.ArgumentParser(description="Photo Wall base bootstrapper (0009)")
    parser.add_argument("--config", type=Path, default=DEFAULT_PUBLIC_CONFIG)
    parser.add_argument("--unit", default=DEFAULT_UNIT)
    parser.add_argument("--cmdline", type=Path, default=KERNEL_COMMAND_LINE)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    clock = RunClockRecord().read()
    try:
        resolution = resolve_central(read_kernel_command_line(args.cmdline))
        transport = HttpTransport(trust=Trust.public())
        discovery: CentralDiscovery | None = None
        if isinstance(resolution, Unconfigured):
            from player.mdns_discovery import MdnsCentralDiscovery

            discovery = MdnsCentralDiscovery(timeout=DISCOVERY_SECONDS)
        # Opt-in per-device `.deb` path (0012 bead 6): only when PHOTO_WALL_PER_DEVICE_DEB is
        # set does the appliance send its serial and hand the served tag forward.
        serial_reader = read_pi_serial if os.environ.get(PER_DEVICE_ENV) == "1" else None
        bootstrapper = Bootstrapper(
            find=functools.partial(find_central, resolution, transport=transport,
                                   discovery=discovery),
            transport=transport, clock=clock,
            write_handoff=functools.partial(write_handoff, path=args.config),
            start_unit=functools.partial(start_player_unit, unit=args.unit),
            serial_reader=serial_reader)
        asyncio.run(bootstrapper.run())
    except UplinkError as error:
        LOG.error("provision: %s", failure_text(error, clock=clock))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
