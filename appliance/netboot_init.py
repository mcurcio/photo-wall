"""Stage 1 of the ticketless netboot (0009 Phase 4; decision 0014): find Central, verify it, fetch
the unsigned rpi-image-gen base squashfs directly from it, and switch_root into it.

This is the initramfs's `python3 -I -m appliance.netboot_init`, run by
`appliance/netboot_initramfs/scripts/photowall-netboot`. Everything it imports is computed into
the initramfs boot data (`scripts/module_closure.py`): this module, `appliance.bootstrap`, the
`uplink` package and the stdlib-only `contracts` modules, never `appliance.provision` or the
Player.

Central-discovery boot model
----------------------------
cmdline.txt carries only Central's ROOT (`photowall.central=`, for example
`http://photo-wall.localdomain/`): no path, nothing that changes when the served squashfs is
revised. `uplink.locate` follows the gateway's redirects from that root to Central's own
origin, over TLS verified against the CA bundle the build copied from the base (R5), and must
end on Central's identity. The base is then one direct request, `NETBOOT_BASE_PATH` on the
located origin, which refuses any redirect. The Pi self-identifies by its hardware serial
(`X-PhotoWall-Serial`, read at boot from the devicetree); the corruption digest arrives in the
`Digest` response header.

Phases, each one console line that also pets the stage-1 watchdog (0014 rev 5, design §2.8);
every network send and read in them pets it too, each one bounded call (`_PacedTransport`):

0. setup (in `main()`): arm the watchdog first, then read the cmdline, load the CA bundle
   (`Trust`) and the clock floor from the boot data. A missing bundle or floor is a broken
   build and stops even an http boot.
1. cmdline: `resolve_central`. Stage 1 has no discovery, so an absent root is a failure.
2. serial.
3. networking: initramfs-tools `configure_networking`.
4. clock: `ClockSettler.settle()` raises the clock to the floor and takes at most one SNTP step
   (R6), before any request.
5. locate: one line per hop, then the located origin (and, for an http root, which https root
   to set).
6. base: `DirectFetch` streams `/v1/netboot/base`; its sha256 must match the `Digest` header, a
   corruption check only (home LAN, no signature on this path). A mismatch, or no `Digest`,
   fails closed with no partial file and nothing mounted.
7. mount and hand off: `LinuxOps.mount_root` (the base's loop device attached by stage 1
   itself, every mount given kernel options only), a note if the base's CA bundle differs
   from this initrd's (R5, Q3 = A), stage 1's working resolver copied onto the new root
   (`hand_over_resolver`: stage 2 has no DHCP client of its own), this initrd's kernel modules
   copied onto the new root (`hand_over_modules`: the base carries none, and the modules travel
   with the kernel they were built for), then the watchdog hand-over to systemd, last.

Every failure prints one `FAILED phase=<n> ...` line and exits non-zero into the boot script's
`photowall_restart`, the one way out. No boot-context file is written: the Player enrolls
ticketless on this root. Every external effect is an injected collaborator, so this is
unit-testable with no root, no network, no clock and no kernel.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import os
import re
import shutil
import socket
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

from appliance.bootstrap import (
    CHUNK,
    BootstrapError,
    BootstrapFatal,
    Keeper,
    LinuxOps,
    arm_watchdog,
    errno_name,
    missing_kernel_liveness,
    open_watchdog,
    read_pi_serial,
)
from contracts.clock_record import ClockRecord
from contracts.release import MAX_ROOTFS_BYTES
from contracts.time import SystemClock
from uplink.causes import Cause, UplinkError
from uplink.clock import (
    DHCP_NTP_SERVERS,
    ClockGate,
    ClockSettler,
    RunClockRecord,
    dhcp_tier,
    pool_tier,
    read_floor,
    step_realtime,
)
from uplink.diagnosis import failure_text, trust_provenance
from uplink.fetch import STATUS_TIMEOUT, DirectFetch
from uplink.files import write_atomically
from uplink.locate import locate
from uplink.lookup import lookup
from uplink.origin import Url
from uplink.resolver import (
    CENTRAL_KEY,
    KERNEL_COMMAND_LINE,
    Unconfigured,
    read_kernel_command_line,
    resolve_central,
)
from uplink.transport import HOP_TIMEOUT, LOOKUP_TIMEOUT, HttpTransport, Reply, Transport
from uplink.trust import DEBIAN_CA_BUNDLE, Trust

RAM_IMAGE_NAME = "photo-wall-base.squashfs"
# The netboot request path is a CODE CONSTANT appended to the located origin, never taken from
# the command line: cmdline.txt carries only Central's root, so the base image stays fleet-wide
# immortal as the served squashfs is revised. The `/v1` prefix is the future-proofing seam.
NETBOOT_BASE_PATH = "/v1/netboot/base"
SERIAL_HEADER = "X-PhotoWall-Serial"
CONSOLE_PATH = "/dev/console"
KMSG_PATH = "/dev/kmsg"
LOG_PREFIX = "photo-wall[netboot]"
PHASES = 7
PROGRESS_INTERVAL = 50 * 1024 * 1024
# The initramfs-tools `configure_networking` shell helper's own bound (0014 rev 5, design §2.8:
# named so S0-AC7's budget/timeout assertions import it rather than a magic 60 in a test).
NETWORKING_TIMEOUT_SECONDS = 60
DEBUG_PAUSE_SECONDS = 60
# Whole-acquisition deadline for the base fetch, DirectFetch's ceiling. Designed against a link
# floor of ~30 Mbit/s: 1 GiB / 30 Mbit ~= 286s. A slower link fails closed (transfer/deadline
# -> restart -> retry), never a truncated mount.
BASE_FETCH_SECONDS = 300
# The longest one stage-1 send may wait for its status line. A send looks the host up, then tries
# every address the lookup returned, each for up to its status_timeout, so uncapped the wait
# before the next pet is a SUM over however many addresses DNS returns (three stalled addresses:
# 7 + 3 x 35 = 112 s, past the watchdog's margin -- a silent reset, no FAILED line, R9). Capped
# at the lookup plus two whole attempts (a dual-stack host's AAAA and A) through the Transport
# contract's absolute `deadline`, so it holds for any Transport; a send that runs out is a named
# CONNECT failure and the FAILED line.
STAGE1_SEND_SECONDS: Final = LOOKUP_TIMEOUT + 2 * STATUS_TIMEOUT
# This initrd's CA bundle (the boot data's copy of the base's), compared after mounting with
# the mounted base's own at the same path (R5, Q3 = A).
INITRD_CA_BUNDLE = DEBIAN_CA_BUNDLE
# Stage 1's resolver: initramfs-tools' configure_networking renders it from the DHCP lease
# (netinfo_to_resolv_conf). The phase-3 line reads it and phase 7 hands it to stage 2 at
# STAGE2_RESOLVER (relative to the new root), bounded: resolv.conf(5) is a few lines.
_STAGE1_RESOLVER: Final = Path("/etc/resolv.conf")
STAGE2_RESOLVER: Final = Path("etc/resolv.conf")
MAX_RESOLVER_BYTES: Final = 4096
# The running kernel's modules, as mkinitramfs staged them into this initrd (the modules and
# depmod's indexes), and where stage 2's udev looks for them, relative to the new root. The base
# carries no kernel and no modules: they come from the same TFTP staging as the kernel, so the
# two always match, whichever base Central serves. The initramfs hook adds the display drivers
# the Player needs (vc4, v3d). scripts/initrd_mount_probe.py imports INITRD_MODULES.
INITRD_MODULES: Final = Path("/usr/lib/modules")
STAGE2_MODULE_DIR: Final = Path("usr/lib/modules")
# The copy lands in the RAM overlay's upper layer: bounded, so a malformed initrd cannot fill
# RAM before stage 2 starts.
MAX_MODULE_TREE_BYTES: Final = 256 * 1024 * 1024


class NetbootError(ValueError):
    """A fixed content-check code (integrity, no digest, empty, chunk, limit); no untrusted
    command-line or HTTP output."""


def _open_log_streams():
    """Best-effort console sinks: `/dev/console` (priority; HDMI/serial) plus
    `/dev/kmsg` (timestamped kernel log). Falls back to stderr when
    `/dev/console` cannot be opened. Logging must never itself fail the boot,
    so every open here is best-effort."""
    streams = []
    try:
        streams.append(open(CONSOLE_PATH, "wb", buffering=0))
    except OSError:
        streams.append(sys.stderr.buffer)
    try:
        streams.append(open(KMSG_PATH, "wb", buffering=0))
    except OSError:
        pass
    return streams


class ConsoleLog:
    """Human-readable, per-phase console logging for field debugging (0009
    Phase-4 owner ask). Writes `photo-wall[netboot] ...` lines directly to the
    Pi console so they surface on HDMI/serial. Best-effort: a broken sink is
    swallowed, never raised, because diagnostics must not fail the boot."""

    def __init__(self, *, debug: bool = False, streams=None):
        self.debug = debug
        self._streams = _open_log_streams() if streams is None else streams

    def _emit(self, message: str) -> None:
        line = f"{LOG_PREFIX} {message}\n".encode("utf-8", "replace")
        for stream in self._streams:
            try:
                stream.write(line)
                flush = getattr(stream, "flush", None)
                if flush is not None:
                    flush()
            except (OSError, ValueError):
                pass

    def info(self, message: str) -> None:
        self._emit(message)

    def detail(self, message: str) -> None:
        """Extra detail emitted only under `photowall.debug`."""
        if self.debug:
            self._emit(message)


def _resolver_entries(text: str) -> tuple[list[str], list[str]]:
    """The nameservers and search domains a resolv.conf names (resolv.conf(5)), in order."""
    nameservers: list[str] = []
    search: list[str] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            nameservers.append(parts[1])
        elif len(parts) >= 2 and parts[0] in ("search", "domain"):
            search.extend(parts[1:])
    return nameservers, search


def _read_small(path: Path) -> str | None:
    try:
        with path.open("rb") as stream:
            return stream.read(4096).decode("ascii", "replace")
    except OSError:
        return None


class NetbootOps(LinuxOps):
    """`LinuxOps` plus network bring-up and console-diagnostic reads.

    Deliberately does NOT add or override `time_ready`, `device_id`, or
    `boot_id` -- this path never enrolls and never verifies a signature, so
    those steps are simply absent rather than stubbed. Name lookup is the
    transport's (`uplink.lookup`, bounded), not an ops step. The stage-1
    hardware watchdog is a `Keeper`, armed once in `main()`. `mount_root`,
    `_prepare_root`, `ram`, `mount`, `loop_device` and `command` are inherited
    from `LinuxOps` unchanged.
    """

    def hand_over_modules(self, rootmnt: Path, *, pet: Callable[[], None],
                          release: str | None = None) -> str:
        return hand_over_modules(rootmnt, pet=pet, release=release)

    def configure_networking(self) -> None:
        # Shells out to the initramfs-tools helper rather than reimplementing DHCP/`ip=`
        # parsing here.
        self.command("sh", "-c", ". /scripts/functions 2>/dev/null; configure_networking",
                     timeout=NETWORKING_TIMEOUT_SECONDS)

    def network_info(self) -> dict[str, str]:
        """Best-effort acquired-IP / gateway / DNS-server / search-domain / option-42 NTP
        summary for the phase-3 console log. Never raises: diagnostics must not fail the
        boot."""
        info: dict[str, str] = {}
        try:
            with open("/proc/net/route") as handle:
                for line in handle.readlines()[1:]:
                    fields = line.split()
                    if len(fields) > 2 and fields[1] == "00000000":
                        gateway = int(fields[2], 16).to_bytes(4, "little")
                        info["gateway"] = socket.inet_ntoa(gateway)
                        info["interface"] = fields[0]
                        break
        except (OSError, ValueError):
            pass
        try:
            # Diagnostic only, but bounded like every other lookup here (M5): plain
            # gethostbyname has no timeout of its own and can hang the boot.
            addresses = lookup(socket.gethostname(), 0, LOOKUP_TIMEOUT)
            if addresses:
                info["ip"] = addresses[0][1]
        except OSError:
            pass
        try:
            nameservers, search = _resolver_entries(_STAGE1_RESOLVER.read_text())
            if nameservers:
                info["dns"] = ",".join(nameservers)
            if search:
                info["search"] = ",".join(search)
        except OSError:
            pass
        ntp = _read_small(DHCP_NTP_SERVERS)
        if ntp is not None:
            info["ntp"] = ",".join(ntp.split()) or "none"
        return info


def _is_truthy(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() not in ("", "0", "false", "no", "off")


_DIGEST_TOKEN = re.compile(r"\s*sha-256\s*=\s*([A-Za-z0-9+/=]+)\s*", re.IGNORECASE)


def parse_digest_header(value: str | None) -> str | None:
    """Parse an RFC-3230 `Digest: sha-256=<base64>` header into the sha256 as
    hex, or None when absent/unparseable.

    Central emits the digest base64-encoded (matching the `.deb` route in
    central/app.py: `base64.b64encode(bytes.fromhex(sha256))`), while the
    corruption gate compares `hashlib.sha256(...).hexdigest()` (hex). Decode
    the base64 to raw bytes and return hex so the comparison is apples to
    apples. A header carrying multiple comma-separated representations is
    scanned for the sha-256 one."""
    if not value:
        return None
    for token in value.split(","):
        match = _DIGEST_TOKEN.fullmatch(token)
        if match is None:
            continue
        try:
            raw = base64.b64decode(match.group(1), validate=True)
        except (binascii.Error, ValueError):
            return None
        return raw.hex() if len(raw) == 32 else None
    return None


def fetch_verified(chunks, destination: Path, expected_digest, *, log=None) -> None:
    """Stream the base squashfs into `destination`, checking its sha256 against
    `expected_digest` as a corruption check only.

    `expected_digest` is the expected sha256 hex string, or a zero-arg callable
    returning it (or None). It is a callable in production because the digest
    arrives in the HTTP `Digest` response header, which the fetch surfaces only
    once the response opens -- so it is resolved AFTER the body has streamed.
    A None result fails closed: this path exists to catch corruption, so a base
    served without a usable `Digest` header is refused, not mounted blindly.

    Same fail-closed discipline as the retired `bootstrap.copy_verified` --
    exclusive create, per-block bound, delete any partial file on any failure."""
    digest = hashlib.sha256()
    created = False
    logged = 0
    try:
        with destination.open("xb") as output:
            created = True
            total = 0
            for block in chunks:
                if not isinstance(block, bytes) or not 0 < len(block) <= CHUNK:
                    raise NetbootError("netboot_chunk")
                total += len(block)
                if total > MAX_ROOTFS_BYTES:
                    raise NetbootError("netboot_limit")
                digest.update(block)
                output.write(block)
                if log is not None and total - logged >= PROGRESS_INTERVAL:
                    logged = total
                    log.info(f"phase 6/{PHASES} base: {total // (1024 * 1024)} MiB")
            if not total:
                raise NetbootError("netboot_empty")
            expected = expected_digest() if callable(expected_digest) else expected_digest
            computed = digest.hexdigest()
            if log is not None:
                log.info(f"phase 6/{PHASES} base: hash compare expected={expected} "
                         f"computed={computed}")
            if expected is None:
                # No usable `Digest` header: the whole point of this path is
                # corruption detection, so refuse rather than mount blind.
                raise NetbootError("netboot_no_digest")
            if computed != expected:
                raise NetbootError("netboot_integrity")
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


def hand_over_resolver(rootmnt: Path, *, source: Path = _STAGE1_RESOLVER) -> str:
    """Copy stage 1's working resolver (at most MAX_RESOLVER_BYTES of it) to
    rootmnt / STAGE2_RESOLVER, mode 0644, replacing whatever the base carries there: a file, or
    a symlink, which is replaced and never followed. Returns the phase-7 console summary
    'dns=<a,b> search=<x>'. A missing source writes an empty file and returns 'dns=none' (an
    IP-literal root still works). A write failure raises OSError: one FAILED line, then the
    restart."""
    try:
        with source.open("rb") as stream:
            data: bytes | None = stream.read(MAX_RESOLVER_BYTES)
    except FileNotFoundError:
        data = None
    write_atomically(rootmnt / STAGE2_RESOLVER, data or b"", mode=0o644)
    if data is None:
        return "dns=none"
    nameservers, search = _resolver_entries(data.decode("ascii", "replace"))
    return f"dns={','.join(nameservers) or 'none'} search={','.join(search) or 'none'}"


def hand_over_modules(rootmnt: Path, *, pet: Callable[[], None], release: str | None = None,
                      source: Path = INITRD_MODULES) -> str:
    """Copy source/<release> (default: the running kernel's, `uname -r`) to
    rootmnt/STAGE2_MODULE_DIR/<release>, symlinks kept, calling `pet` after every file, so stage
    2's udev can load a driver by its alias at coldplug. Returns the phase-7 console summary
    'modules=<release> files=<n> bytes=<b>'. No tree for the running kernel (a kernel staged
    with another build's initrd) or a tree over MAX_MODULE_TREE_BYTES raises
    BootstrapError('boot_modules', ...) naming it; a failed copy names its errno."""
    release = release or os.uname().release
    tree = source / release
    if not tree.is_dir():
        raise BootstrapError("boot_modules", f"no {tree} in this initrd (kernel and initrd "
                                             "from different builds)")
    copied = {"files": 0, "bytes": 0}

    def copy(src: str, dst: str) -> str:
        copied["bytes"] += os.lstat(src).st_size
        if copied["bytes"] > MAX_MODULE_TREE_BYTES:
            raise BootstrapError("boot_modules", f"tree over {MAX_MODULE_TREE_BYTES} bytes")
        copied["files"] += 1
        result = shutil.copy2(src, dst)
        pet()
        return result

    try:
        shutil.copytree(tree, rootmnt / STAGE2_MODULE_DIR / release, symlinks=True,
                        copy_function=copy, dirs_exist_ok=True)
    except shutil.Error as error:
        # copytree collects per-file failures and raises them together: name the first.
        failures = error.args[0] if error.args and isinstance(error.args[0], list) else []
        raise BootstrapError("boot_modules",
                             str(failures[0][2]) if failures else str(error)) from None
    except OSError as error:
        raise BootstrapError("boot_modules", errno_name(error)) from None
    return f"modules={release} files={copied['files']} bytes={copied['bytes']}"


def _sha256_of(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def compare_trust_bundles(initrd_bundle: Path, base_bundle: Path) -> tuple[str, str] | None:
    """None when the files are byte-equal; otherwise (initrd_sha256, base_sha256).
    A missing base bundle counts as a difference, with 'absent' in place of its sha256."""
    initrd, base = _sha256_of(initrd_bundle), _sha256_of(base_bundle)
    if initrd is not None and initrd == base:
        return None
    return initrd or "absent", base or "absent"


def failure_line(phase: str, error: BaseException, *, clock: ClockRecord | None = None,
                 provenance: str = "") -> str:
    """The one FAILED line: `FAILED phase=<n> ` + uplink.diagnosis.failure_text for a named
    cause, so a `time` or `tls`/`untrusted` failure carries the clock record's summary and the
    trust provenance. Stage 1's own content codes and the mount's fixed codes print as
    `code=<code>`, a mount-step failure followed by ` detail=<its cause>` (a helper's stderr
    tail or an errno name; one line, the rest of the line); anything else only by its type."""
    if isinstance(error, UplinkError):
        text = failure_text(error, clock=clock, provenance=provenance)
    elif isinstance(error, (NetbootError, BootstrapError, BootstrapFatal)):
        text = f"code={error}"
        if isinstance(error, BootstrapError) and error.detail:
            text += f" detail={error.detail}"
    else:
        text = f"error={type(error).__name__}"
    return f"FAILED phase={phase} {text}"


def _pre_reboot_pause(debug: bool, log) -> None:
    if not debug:
        # The shell wrapper's own `sleep` covers the normal path; only the
        # debug flag lengthens the pause so a human can read the console.
        return
    log.info(f"debug: pausing {DEBUG_PAUSE_SECONDS}s before reboot so this console is readable")
    try:
        time.sleep(DEBUG_PAUSE_SECONDS)
    except BaseException:
        pass


class _Console:
    """The phase lines and the one FAILED line. Every line it writes pets the watchdog, so a
    phase cannot be added without a pet and the debug pause after a failure starts from a
    fresh pet (0014 rev 5, design §2.8)."""

    def __init__(self, log, keeper: Keeper, *, provenance: str = "") -> None:
        self.log, self.keeper, self.provenance = log, keeper, provenance
        self.phase = "setup"
        self.clock: ClockRecord | None = None

    def begin(self, phase: int) -> None:
        """Name the phase a failure from here on belongs to, before its line is due."""
        self.phase = str(phase) if phase else "setup"

    def line(self, phase: int, message: str) -> None:
        self.begin(phase)
        self.log.info(f"phase {phase}/{PHASES} {message}")
        self.keeper.pet()

    def failed(self, error: BaseException, *, debug: bool) -> None:
        self.log.info(failure_line(self.phase, error, clock=self.clock,
                                   provenance=self.provenance))
        self.keeper.pet()
        _pre_reboot_pause(debug, self.log)


class _PacedReply:
    """A Reply that pets the keeper after every read that returns: a read is one bounded wait
    (its caller's `timeout`), so a body drained in many reads -- a slow error body included --
    is a run of short gaps, never one long one."""

    __slots__ = ("_reply", "_keeper")

    def __init__(self, reply: Reply, keeper: Keeper) -> None:
        self._reply, self._keeper = reply, keeper

    @property
    def status(self) -> int:
        return self._reply.status

    @property
    def headers(self) -> Mapping[str, str]:
        return self._reply.headers

    @property
    def peer(self) -> str:
        return self._reply.peer

    def read(self, amount: int, *, timeout: float) -> bytes:
        block = self._reply.read(amount, timeout=timeout)
        self._keeper.pet()
        return block

    def close(self) -> None:
        self._reply.close()


class _PacedTransport:
    """Stage 1's only Transport (0014 rev 5, design §2.8): every network wait is ONE bounded call
    followed by a pet. A send's absolute deadline is held to STAGE1_SEND_SECONDS from now, and
    its reply pets per read, so no gap between two pets is a sum over addresses or reads. A call
    that raises does not pet: the FAILED line does."""

    __slots__ = ("_transport", "_keeper", "_monotonic")

    def __init__(self, transport: Transport, keeper: Keeper, *,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._transport, self._keeper, self._monotonic = transport, keeper, monotonic

    def send(self, url: Url, *, headers: Mapping[str, str], deadline: float,
             status_timeout: float = HOP_TIMEOUT) -> Reply:
        reply = self._transport.send(
            url, headers=headers, status_timeout=status_timeout,
            deadline=min(deadline, self._monotonic() + STAGE1_SEND_SECONDS))
        self._keeper.pet()
        return _PacedReply(reply, self._keeper)


def netboot(cmdline: Mapping[str, str] | None, rootmnt: Path, *, ops: NetbootOps,
            transport: Transport, clock_gate: ClockSettler, keeper: Keeper,
            trust_provenance: str, serial_reader: Callable[[], str | None],
            log: ConsoleLog) -> None:
    """Locate Central, fetch and RAM-overlay-mount the base, hand the watchdog over. Every
    collaborator is required, with no defaults: a test that forgets a fake fails with
    TypeError instead of reaching the real network or clock. Only main() builds the real ones.
    On any failure: one FAILED line (which pets `keeper`), the debug pause if
    `photowall.debug`, then the error propagates -- never a shell, always the boot script's
    restart."""
    debug = _is_truthy((cmdline or {}).get("photowall.debug"))
    console = _Console(log, keeper, provenance=trust_provenance)
    try:
        _run_netboot(cmdline, rootmnt, console, ops=ops,
                     transport=_PacedTransport(transport, keeper),
                     clock_gate=clock_gate, serial_reader=serial_reader)
    except BaseException as error:
        console.failed(error, debug=debug)
        raise


def _run_netboot(cmdline, rootmnt: Path, console: _Console, *, ops, transport: Transport,
                 clock_gate: ClockSettler, serial_reader) -> None:
    missing = missing_kernel_liveness()
    note = f" note: kernel liveness missing: {' '.join(missing)}" if missing else ""
    console.line(0, f"setup: {console.provenance} keeper={console.keeper.summary}{note}")

    # Phase 1: which Central (R1). Stage 1 has no discovery: absent is a failure.
    params = {key: value for key, value in (cmdline or {}).items()
              if key.startswith("photowall.")}
    console.line(1, f"cmdline: {CENTRAL_KEY}={params.get(CENTRAL_KEY)!r} params={params}")
    resolved = resolve_central(cmdline)
    if isinstance(resolved, Unconfigured):
        raise UplinkError(Cause.CONFIGURATION, "absent",
                          detail="" if resolved.reason == "absent" else resolved.reason)
    root = resolved.root

    # Phase 2: serial (self-supplied identity; nothing baked).
    serial = serial_reader()
    if serial is None:
        console.line(2, "serial: UNAVAILABLE (no firmware serial-number; Central serves the "
                        "default base)")
    else:
        console.line(2, f"serial: {serial}")

    # Phase 3: networking up.
    console.begin(3)
    try:
        ops.configure_networking()
    except BootstrapError as error:
        raise UplinkError(Cause.CONNECT, "unreachable", host=root.host,
                          detail="network_setup") from error
    info = ops.network_info()
    console.line(3, f"networking up: ip={info.get('ip', '?')} gateway={info.get('gateway', '?')} "
                    f"interface={info.get('interface', '?')} dns={info.get('dns', '?')} "
                    f"search={info.get('search', '?')}")
    console.log.detail(f"phase 3/{PHASES} option 42: ntp_servers={info.get('ntp', '?')}")

    # Phase 4: the clock, before any request (R6), for http and https roots alike.
    console.begin(4)
    record = clock_gate.settle()
    console.clock = record
    console.line(4, f"clock: {record.summary()} raised={record.raised_to_floor} "
                    f"tier={record.tier} source={record.source} offset={record.offset} "
                    f"stepped={record.stepped}")

    # Phase 5: locate through the gateway's redirects to Central's own origin (R3, R7, R8).
    def hop(url: Url, status: int, peer: str) -> None:
        console.line(5, f"locate: GET {url} -> {status} peer={peer}")

    console.begin(5)
    located = locate(root, transport=transport, on_hop=hop)
    console.line(5, f"located {located.origin} (Central api {located.identity.api})")
    if root.scheme == "http":
        # Keyed on the CONFIGURED root: it decides how far the first answer can be trusted.
        # U7: never suggest a value to pin here -- the located origin can come from an
        # unauthenticated first hop, so this note names the risk only.
        console.log.info("note: configured root is http: the first hop is unauthenticated")

    # Phase 6: the base, one direct request to the located origin.
    headers = {SERIAL_HEADER: serial} if serial else {}
    console.line(6, f"base: GET {located.origin.url(NETBOOT_BASE_PATH)} headers={headers}")
    fetcher = DirectFetch(located, transport=transport, seconds=BASE_FETCH_SECONDS)
    image = ops.ram() / RAM_IMAGE_NAME
    captured: dict[str, str | None] = {}

    def on_response(response_headers: Mapping[str, str]) -> None:
        # The expected digest arrives with the 200's headers, before the body; fetch_verified
        # resolves it after streaming.
        raw_digest = response_headers.get("Digest")
        captured["digest"] = parse_digest_header(raw_digest)
        console.line(6, f"base: 200 Digest={raw_digest!r} "
                        f"Content-Length={response_headers.get('Content-Length')!r}")
        if captured["digest"] is None:
            console.log.info(f"phase 6/{PHASES} base: NO usable sha-256 Digest header -- "
                             "the fetch will fail closed")

    try:
        chunks = fetcher.chunks(NETBOOT_BASE_PATH, MAX_ROOTFS_BYTES, block=CHUNK,
                                headers=headers, on_response=on_response)
        # keeper.paced pets once per streamed block (S0-AC4).
        fetch_verified(console.keeper.paced(chunks), image, lambda: captured.get("digest"),
                       log=console.log)
        console.line(7, "mount + handoff: mounting squashfs")
        ops.mount_root(image, rootmnt)
        differs = compare_trust_bundles(INITRD_CA_BUNDLE,
                                        rootmnt / DEBIAN_CA_BUNDLE.relative_to("/"))
        if differs is not None:
            console.log.info(f"note: CA bundle differs from the base's: initrd=sha256:"
                             f"{differs[0]} base=sha256:{differs[1]}")
        resolver = hand_over_resolver(rootmnt)
        console.line(7, "mount + handoff: "
                        + ops.hand_over_modules(rootmnt, pet=console.keeper.pet))
        console.line(7, f"mount + handoff: success {resolver}")
        console.keeper.hand_over()
    except BaseException:
        image.unlink(missing_ok=True)
        raise


def main() -> None:
    """First, before anything else: keeper = arm_watchdog(device=open_watchdog()) (§2.8).
    Phase 0 'setup' then reads the cmdline and builds Trust.public(), HttpTransport and
    ClockGate(read_floor(), [dhcp_tier(), pool_tier()], SystemClock(), step_realtime,
    RunClockRecord(), writer="netboot"), then calls netboot(). A failure in setup prints the
    same FAILED line as any phase (phase=setup). Every failure exits non-zero into the boot
    script's photowall_restart, the one way out (§2.8)."""
    keeper = arm_watchdog(device=open_watchdog())
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rootmnt", type=Path, default=Path("/root"))
    parser.add_argument("--cmdline", type=Path, default=KERNEL_COMMAND_LINE)
    args = parser.parse_args()
    log = ConsoleLog()
    console = _Console(log, keeper)
    cmdline = None
    try:
        cmdline = read_kernel_command_line(args.cmdline)
        log.debug = _is_truthy((cmdline or {}).get("photowall.debug"))
        trust = Trust.public()
        floor = read_floor()
        ops = NetbootOps()
    except (UplinkError, OSError, BootstrapError) as error:
        console.failed(error, debug=log.debug)
        raise SystemExit("photo-wall: netboot_failed") from None
    clock_gate = ClockGate(floor=floor, tiers=[dhcp_tier(), pool_tier()], clock=SystemClock(),
                           step=step_realtime, store=RunClockRecord(), writer="netboot")
    try:
        netboot(cmdline, args.rootmnt, ops=ops, transport=HttpTransport(trust=trust),
                clock_gate=clock_gate, keeper=keeper,
                trust_provenance=trust_provenance(trust, floor), serial_reader=read_pi_serial,
                log=log)
    except (NetbootError, UplinkError, BootstrapError, BootstrapFatal, OSError):
        raise SystemExit("photo-wall: netboot_failed") from None


if __name__ == "__main__":
    main()
