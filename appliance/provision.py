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
6. starts `photo-wall-player.service`. A start that fails is one line naming how the unit's
   process ended, `cause=unit reason=<systemd Result> detail=<unit>/status=216/GROUP` (R9), and
   the process exits 1 into the unit's start limit.

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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, TypeVar

from appliance.app_executor import AppExecutor, ExecutorError, expected_abi
from appliance.boot_offer import BOOT_HANDOFF, BootOfferError, read_current_handoff
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
# creates the `wall` user. Unmeasured on a Pi 5 and deliberately generous until it is: a bound
# that is too short breaks provisioning outright, while a generous one only slows detection of a
# hung dpkg. Tighten once a real Pi 5 install time is measured.
INSTALL_SECONDS: Final = 300.0
# `systemctl start` (subprocess timeout) waits for the Player's READY=1 (Type=notify).
# photo-wall-player.service sets no TimeoutStartSec, so systemd fails that start itself after its
# DefaultTimeoutStartSec, 90 s: waiting longer here would wait on a start systemd gave up on.
START_UNIT_SECONDS: Final = 90.0
# `systemctl show` after a failed start (subprocess timeout): one property read from PID 1.
# Not a term of LONGEST_ATTEMPT_SECONDS: it runs only on the path that exits instead of backing
# off, so it spends part of the MAX_BACKOFF_SECONDS that path never sleeps (a test holds it
# below that).
UNIT_STATUS_SECONDS: Final = 5.0
MAX_BACKOFF_SECONDS: Final = 30.0
# A read-only app sample can hold the executor's shared lock past its OS-agent
# reporting deadline. Bound each wait; production keeps waiting for the lock.
EXECUTOR_BUSY_RETRY_SECONDS: Final = 1.0
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


_ExecutorResult = TypeVar("_ExecutorResult")


# systemd's own exit statuses: the unit's process failed while systemd was setting it up
# (user, groups, namespaces, directories, exec), before the program itself ran. Named as
# `systemd-analyze exit-status` prints them for systemd 257, the base's (systemd.exec(5),
# "Process Exit Codes").
SYSTEMD_EXIT_STATUSES: Final[Mapping[int, str]] = MappingProxyType({
    200: "CHDIR", 201: "NICE", 202: "FDS", 203: "EXEC", 204: "MEMORY", 205: "LIMITS",
    206: "OOM_ADJUST", 207: "SIGNAL_MASK", 208: "STDIN", 209: "STDOUT", 210: "CHROOT",
    211: "IOPRIO", 212: "TIMERSLACK", 213: "SECUREBITS", 214: "SETSCHEDULER",
    215: "CPUAFFINITY", 216: "GROUP", 217: "USER", 218: "CAPABILITIES", 219: "CGROUP",
    220: "SETSID", 221: "CONFIRM", 222: "STDERR", 224: "PAM", 225: "NETWORK", 226: "NAMESPACE",
    227: "NO_NEW_PRIVILEGES", 228: "SECCOMP", 229: "SELINUX_CONTEXT", 230: "PERSONALITY",
    231: "APPARMOR", 232: "ADDRESS_FAMILIES", 233: "RUNTIME_DIRECTORY", 235: "CHOWN",
    236: "SMACK_PROCESS_LABEL", 237: "KEYRING", 238: "STATE_DIRECTORY", 239: "CACHE_DIRECTORY",
    240: "LOGS_DIRECTORY", 241: "CONFIGURATION_DIRECTORY", 242: "NUMA_POLICY",
    243: "CREDENTIALS", 244: "BPF", 245: "KSM",
})
# What `systemctl show` reports about how a unit's start went.
UNIT_PROPERTIES: Final = ("ActiveState", "SubState", "Result", "ExecMainCode", "ExecMainStatus")
_UNIT_TOKEN = re.compile(r"[A-Za-z0-9_.@-]{1,64}")
_CLD_EXITED, _CLD_SIGNALLED = "1", ("2", "3")   # ExecMainCode: exited; killed or dumped


def parse_unit_properties(text: str) -> dict[str, str]:
    """PURE. `systemctl show --property=...` output, `Key=Value` per line, as a dict."""
    properties = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            properties[key.strip()] = value.strip()
    return properties


def unit_ending(properties: Mapping[str, str]) -> str:
    """PURE. How the unit's main process ended, as one token: `status=216/GROUP` (systemd's
    own status, named), `status=1` (the program's), `signal=9`, or `status=unknown`."""
    code, status = properties.get("ExecMainCode", ""), properties.get("ExecMainStatus", "")
    if not status.isdigit():
        return "status=unknown"
    if code in _CLD_SIGNALLED:
        return f"signal={status}"
    name = SYSTEMD_EXIT_STATUSES.get(int(status)) if code == _CLD_EXITED else None
    return f"status={status}/{name}" if name else f"status={status}"


class UnitStartError(RuntimeError):
    """The Player unit did not start (R9). str() is one line in provisioning's shape:
      a recorded failure   `cause=unit reason=<systemd's Result> detail=<unit>/<unit_ending>`,
                           e.g. `reason=exit-code
                           detail=photo-wall-player.service/status=216/GROUP`;
      a start that outlived START_UNIT_SECONDS
                           `cause=unit reason=timeout detail=<unit>/state=<ActiveState>/<SubState>`;
      any other start that did not finish (systemd recorded no failure: Result=success)
                           `cause=unit reason=unfinished detail=<unit>/state=<...>/<...>`.
    The unit still starting has no exit status to name, so its state is the detail."""

    def __init__(self, unit: str, properties: Mapping[str, str], *,
                 timed_out: bool = False) -> None:
        def token(key: str) -> str:
            value = properties.get(key, "")
            return value if _UNIT_TOKEN.fullmatch(value) else "unknown"

        name = unit if _UNIT_TOKEN.fullmatch(unit) else "unit"
        result = token("Result")
        if timed_out or result == "success":
            reason = "timeout" if timed_out else "unfinished"
            detail = f"{name}/state={token('ActiveState')}/{token('SubState')}"
        else:
            reason, detail = result, f"{name}/{unit_ending(properties)}"
        super().__init__(f"cause=unit reason={reason} detail={detail}")


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


def offered_manifest(handoff: Mapping[str, object]) -> AppManifest | None:
    """Use the stage-one selection verbatim; never consult a mutable manifest."""
    if handoff.get("mode") != "offer":
        return None
    asset = handoff.get("initial_app")
    if asset is None:
        return None
    if not isinstance(asset, dict):
        raise ProvisionError("boot_handoff_app_invalid")
    return AppManifest(str(asset["tag"]), str(asset["sha256"]), int(asset["size"]),
                       str(asset["tag"]))


def fetch_offered_package(fetch: DirectFetch, manifest: AppManifest, offer_id: str) -> bytes:
    package = b"".join(fetch.chunks(f"/v1/netboot/offers/{offer_id}/app", manifest.size,
                                  block=CHUNK))
    if len(package) != manifest.size:
        raise ProvisionError("offered_app_size_mismatch")
    return package


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
    installed and the handoff is written, waiting at most START_UNIT_SECONDS. A start that
    fails or outlives that raises UnitStartError, named from the unit's own `systemctl show`
    (read within UNIT_STATUS_SECONDS; unreadable properties name nothing rather than hide the
    failure). Tests inject a stub instead of shelling to real `systemctl`."""
    try:
        subprocess.run(["systemctl", "start", unit], check=True, timeout=START_UNIT_SECONDS)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        try:
            shown = subprocess.run(
                ["systemctl", "show", *(f"--property={name}" for name in UNIT_PROPERTIES), unit],
                capture_output=True, text=True, timeout=UNIT_STATUS_SECONDS, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            shown = ""
        raise UnitStartError(unit, parse_unit_properties(shown or ""),
                             timed_out=isinstance(error, subprocess.TimeoutExpired)) from error


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
                 watchdog_ready: Callable[[], bool] = watchdog_ready,
                 boot_handoff: Mapping[str, object] | None = None,
                 phase: Callable[[str, str | None, str | None], None] | None = None,
                 data_executor: AppExecutor | None = None,
                 base_abi_reader: Callable[[], str] = expected_abi) -> None:
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
        self._boot_handoff = boot_handoff
        self._phase = phase or (lambda _phase, _digest, _fault: None)
        self._data_executor = data_executor or AppExecutor()
        self._base_abi_reader = base_abi_reader

    def _serial(self) -> str | None:
        if self._serial_reader is None:
            return None
        try:
            return self._serial_reader()
        except OSError:
            return None

    def _fetch(self, found: Found, seconds: float) -> DirectFetch:
        return DirectFetch(found.central, transport=self._transport, seconds=seconds)

    async def _retry_executor_busy(self, effect: Callable[[], _ExecutorResult], *,
                                   max_attempts: int | None, digest: str | None,
                                   resume_phase: str | None = None) -> _ExecutorResult:
        """Wait through a local observer's shared lock without reselecting inputs."""
        attempts = 0
        while max_attempts is None or attempts < max_attempts:
            attempts += 1
            if attempts > 1 and resume_phase is not None:
                self._phase(resume_phase, digest, None)
            try:
                return effect()
            except ExecutorError as error:
                if str(error) != "executor_busy" or attempts == max_attempts:
                    raise
                self._phase("retry_wait", digest, "executor_busy")
                # A collector may outlive its reporting deadline. Keep the unit's
                # start window alive while polling the nonblocking exclusive lock.
                self._watchdog_extend(PROVISION_ATTEMPT_TIMEOUT_SECONDS)
                await self._sleep(EXECUTOR_BUSY_RETRY_SECONDS)
        raise AssertionError("executor retry budget must be positive")

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
            if attempt == 1 and self._boot_handoff and self._boot_handoff.get("schema") == 2:
                # Repair an interrupted local activation before any dependency on Central.
                # The selected root and journal are volatile but survive this unit restarting
                # within the same PXE boot. A stale boot handoff is rejected by main().
                try:
                    base_abi = self._base_abi_reader()
                    recovered = await self._retry_executor_busy(
                        lambda: self._data_executor.recover(expected_base_abi=base_abi),
                        max_attempts=max_attempts, digest=None)
                except ExecutorError as error:
                    fault = str(error)
                    if re.fullmatch(r"[a-z0-9_]{1,64}", fault) is None:
                        fault = "app_recovery_failed"
                    self._phase("retry_wait", None, fault)
                    if fault == "executor_busy":
                        LOG.warning("provision: local app recovery still waiting for observer")
                    else:
                        LOG.error("provision: local app recovery requires repair: %s", error)
                    self._watchdog_ready()
                    return False
                if recovered is not None:
                    LOG.info("provision: locally recovered app %s (%s)",
                             recovered.active_sha256, recovered.state)
            if (self._boot_handoff and self._boot_handoff.get("mode") == "offer"
                    and self._boot_handoff.get("initial_app") is None):
                status = self._boot_handoff.get("initial_app_status")
                fault = {"unconfigured": "app_unconfigured",
                         "unavailable": "app_unavailable",
                         "compatibility_unverified": "compatibility_unverified"}.get(
                             status, "app_selection_invalid")
                self._phase("retry_wait", None, fault)
                self._watchdog_ready()
                return False
            manifest: AppManifest | None = None
            try:
                found = await self._find()
                LOG.info("provision: central %s (%s root %s)", found.central.origin,
                         found.source, found.root)
                if self._boot_handoff and self._boot_handoff.get("mode") == "offer":
                    manifest = offered_manifest(self._boot_handoff)
                    assert manifest is not None
                    if self._boot_handoff.get("schema") == 2:
                        asset = self._boot_handoff["initial_app"]
                        expected_base_abi = self._base_abi_reader()
                        if (not isinstance(asset, dict)
                                or asset.get("format") != "pw-player-data-v1"
                                or asset.get("base_abi") != expected_base_abi):
                            raise ProvisionError("data_payload_abi_mismatch")
                    self._phase("fetching_app", manifest.sha256, None)
                    package = fetch_offered_package(self._fetch(found, PACKAGE_SECONDS),
                                                    manifest, str(self._boot_handoff["offer_id"]))
                else:
                    self._phase("fetching_app", None, None)
                    manifest = self._fetch_manifest(self._fetch(found, MANIFEST_SECONDS),
                                                    self._serial())
                    package = self._fetch_package(self._fetch(found, PACKAGE_SECONDS), manifest)
            except UplinkError as error:
                if (error.cause is Cause.TIME or
                        error.central_error == "boot_offer_expired"):
                    raise
                self._phase("retry_wait", manifest.sha256 if manifest else None,
                            error.central_error or "uplink_unavailable")
                LOG.warning("provision: %s (attempt %d)",
                            failure_text(error, clock=self._clock), attempt)
                await self._sleep(self._backoff(attempt))
                continue
            except ProvisionError as error:
                self._phase("retry_wait", manifest.sha256 if manifest else None, str(error))
                LOG.warning("provision: %s (attempt %d)", error, attempt)
                await self._sleep(self._backoff(attempt))
                continue
            except ExecutorError as error:
                self._phase("retry_wait", manifest.sha256 if manifest else None,
                            "base_abi_unavailable")
                LOG.error("provision: base executor unavailable: %s", error)
                self._watchdog_ready()
                return False
            self._phase("verifying_app", manifest.sha256, None)
            if hashlib.sha256(package).hexdigest() != manifest.sha256:
                # Corruption guard (0009 owner ruling: integrity, not authenticity). Never
                # install or start on a mismatch.
                LOG.warning("provision: app_integrity mismatch, discarding (attempt %d)", attempt)
                self._phase("retry_wait", manifest.sha256, "app_integrity")
                await self._sleep(self._backoff(attempt))
                continue
            self._phase("installing_app", manifest.sha256, None)
            if self._boot_handoff and self._boot_handoff.get("schema") == 2:
                asset = self._boot_handoff["initial_app"]
                assert isinstance(asset, dict)
                # The OS channel owns base health for schema 2. Suppress the
                # legacy app-owned base-health report, even when a base tag is
                # available; an app tag may belong to another release cohort.
                base_tag = self._boot_handoff.get("base_tag")
                if not isinstance(base_tag, str) or not base_tag:
                    self._phase("retry_wait", manifest.sha256, "boot_handoff_base_tag_invalid")
                    self._watchdog_ready()
                    return False
                self._write_handoff(Handoff(found.root if found.source == "discovered" else None,
                                            None))
                try:
                    base_abi = str(asset["base_abi"])
                    offer_id = str(self._boot_handoff["offer_id"])
                    outcome = await self._retry_executor_busy(
                        lambda: self._data_executor.activate(
                            package, sha256=manifest.sha256, size=manifest.size,
                            base_abi=base_abi, attempt_id=offer_id,
                            expected_base_abi=expected_base_abi),
                        max_attempts=max_attempts, digest=manifest.sha256,
                        resume_phase="installing_app")
                except ExecutorError as error:
                    fault = str(error)
                    if re.fullmatch(r"[a-z0-9_]{1,64}", fault) is None:
                        fault = "app_activation_failed"
                    self._phase("retry_wait", manifest.sha256, fault)
                    if fault == "executor_busy":
                        LOG.warning("provision: data app %s still waiting for observer",
                                    manifest.sha256)
                    else:
                        LOG.error("provision: data app %s awaiting operator: %s",
                                  manifest.sha256, error)
                    self._watchdog_ready()
                    return False
                if outcome != "committed":
                    self._phase("retry_wait", manifest.sha256, "app_attempt_rolled_back")
                    self._watchdog_ready()
                    return False
            else:
                self._install(package, manifest)
                self._write_handoff(Handoff(found.root if found.source == "discovered" else None,
                                            manifest.tag))
                self._phase("starting_app", manifest.sha256, None)
                self._start_unit()
            self._phase("player_unit_started", manifest.sha256, None)
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
    failure_text, and a UnitStartError as its own line; either exits 1 (systemd restarts; 10
    exits in 10 minutes reboot the Pi)."""
    parser = argparse.ArgumentParser(description="Photo Wall base bootstrapper (0009)")
    parser.add_argument("--config", type=Path, default=DEFAULT_PUBLIC_CONFIG)
    parser.add_argument("--unit", default=DEFAULT_UNIT)
    parser.add_argument("--cmdline", type=Path, default=KERNEL_COMMAND_LINE)
    parser.add_argument("--boot-handoff", type=Path, default=BOOT_HANDOFF)
    parser.add_argument("--boot-id", type=Path, default=Path("/proc/sys/kernel/random/boot_id"))
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
        from appliance.os_agent import write_phase

        boot_id = args.boot_id.read_text().strip()
        try:
            boot_handoff = read_current_handoff(args.boot_handoff, boot_id)
        except BootOfferError as error:
            LOG.error("provision: %s", error)
            write_phase("retry_wait", None, str(error), boot_id_path=args.boot_id)
            watchdog_ready()
            return
        if boot_handoff is None:
            # An older stage-one initrd never wrote this file. Such a boot is explicitly
            # uncorrelated and retains the legacy manifest path for mixed-version rollout.
            boot_handoff = {"mode": "legacy_uncorrelated"}

        bootstrapper = Bootstrapper(
            find=functools.partial(find_central, resolution, transport=transport,
                                   discovery=discovery),
            transport=transport, clock=clock,
            write_handoff=functools.partial(write_handoff, path=args.config),
            start_unit=functools.partial(start_player_unit, unit=args.unit),
            serial_reader=serial_reader, boot_handoff=boot_handoff,
            phase=write_phase)
        asyncio.run(bootstrapper.run())
    except UplinkError as error:
        LOG.error("provision: %s", failure_text(error, clock=clock))
        raise SystemExit(1) from None
    except UnitStartError as error:
        LOG.error("provision: %s", error)
        raise SystemExit(1) from None
    except ExecutorError as error:
        LOG.error("provision: %s", error)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
