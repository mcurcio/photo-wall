"""Ticketless netboot init for the rpi-image-gen base (0009 Phase 4, slice
p4-boot-chain part 1: docs/decisions/0009-minimal-base-and-app-package.md,
"Phase 4 boot-chain + retirement plan").

This is a NEW, additive boot path. It does not touch, replace, or retire
`appliance/bootstrap.py`'s `boot()` (the ticket/signed-rootfs flow) -- that
stays live for the existing netboot tier and its e2e coverage. This module
boots the *unsigned* rpi-image-gen base squashfs instead: no boot ticket, no
signature verification, and no trial watchdog.

Central-discovery boot model
----------------------------
The base netboot image is fleet-wide immortal: cmdline.txt carries only
Central's ROOT URL (`photowall.central=`, e.g. `http://photo-wall/`) -- no
subpath, no query, nothing that changes when the squashfs is revised. The
concrete request path is a code constant appended by this initrd
(`NETBOOT_BASE_PATH = "/v1/netboot/base"`, the version prefix being the
future-proofing seam), NOT taken from the command line. The Pi self-identifies
by its hardware serial (`X-PhotoWall-Serial` request header, read at boot from
`/sys/firmware/devicetree/base/serial-number` -- nothing is baked into the
image); Central decides and serves the right image for that serial. The
expected corruption digest arrives with the bytes, in the HTTP `Digest`
response header, not on the command line.

What it does, in order (each phase logged to the console -- see `ConsoleLog`):

1. Reads Central's ROOT from the kernel command line (`photowall.central=`),
   set by the operator's boot server, never baked into any image. An optional
   `photowall.debug=1` raises verbosity and lengthens the pre-reboot pause on
   failure so a human at an HDMI/serial console can read it.
2. Reads the Pi's hardware serial (self-supplied identity).
3. Brings up networking (initramfs-tools `configure_networking`, as the
   ticketed path does) and logs the acquired IP, gateway, and DNS config.
4. Resolves Central's hostname (the #1 field failure point -- logged loudly).
5. Fetches the base squashfs over HTTP from `<central>/v1/netboot/base`,
   sending the serial header, using `appliance.provision.AppFetcher` (bounded
   deadline, no proxies/redirects, exact `Content-Length` bound, streamed
   reads). Integrity is a **corruption check only** (0009: home LAN, no
   signature anywhere on this path): the downloaded bytes' sha256 is compared
   against the `Digest: sha-256=<base64>` response header, incrementally, and
   any mismatch -- or a missing `Digest` header -- fails closed, leaving no
   partial file and mounting nothing.
6. RAM-overlay-mounts the fetched squashfs by calling
   `appliance.bootstrap.LinuxOps.mount_root` **unchanged** (`NetbootOps`
   subclasses `LinuxOps` and adds only `configure_networking`/`network_info`/
   `resolve` -- `mount_root`/`_prepare_root`/`ram`/`command` are inherited).
7. Writes NO boot-context file at all: `player.service.resolve_boot_context`
   falls back to `hardware_boot_context()` (`ticket_id=None`) whenever that
   file is absent, which is what makes the app enroll ticketless on this root.

Every external effect -- reading the command line, bringing up networking, DNS
resolution, the HTTP fetch, and the mount -- is an injected callable/object, so
this is unit-testable with no root, no real network, and no kernel.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import os
import re
import socket
import sys
import time
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from appliance.bootstrap import CHUNK, BootstrapFatal, LinuxOps, read_pi_serial
from appliance.provision import MAX_APP_PACKAGE_BYTES, AppFetcher, ProvisionError
from contracts.release import MAX_ROOTFS_BYTES

# The base fetch passes MAX_ROOTFS_BYTES as AppFetcher's `maximum`, which the
# fetcher requires to be <= MAX_APP_PACKAGE_BYTES. Assert the ordering at import
# so nudging either cap fails the initrd loudly at boot (it runs `python3 -I`,
# not `-O`, so this assert is live) rather than silently making every base fetch
# raise `provision_limit` before a byte is read.
assert MAX_ROOTFS_BYTES <= MAX_APP_PACKAGE_BYTES

RAM_IMAGE_NAME = "photo-wall-base.squashfs"
# The netboot request path is a CODE CONSTANT appended by the initrd, never
# taken from the command line: cmdline.txt carries only Central's root, so the
# base image stays fleet-wide immortal as the served squashfs is revised. The
# `/v1` prefix is the future-proofing seam for a protocol revision.
NETBOOT_BASE_PATH = "/v1/netboot/base"
SERIAL_HEADER = "X-PhotoWall-Serial"
CONSOLE_PATH = "/dev/console"
KMSG_PATH = "/dev/kmsg"
LOG_PREFIX = "photo-wall[netboot]"
PROGRESS_INTERVAL = 50 * 1024 * 1024
DEBUG_PAUSE_SECONDS = 60
# Whole-acquisition deadline for the base fetch. AppFetcher's default is 60s and
# its hard ceiling is 300s; a <=1 GiB base needs more than 60s on a slow LAN, and
# its size is no longer on the (now static) cmdline, so it cannot be sized
# per-boot. Designed against a link floor of ~30 Mbit/s: 1 GiB / 30 Mbit ~= 286s,
# under the 300s ceiling. A slower link fails closed (provision_deadline ->
# reboot -> retry), never a truncated mount.
BASE_FETCH_SECONDS = 300


class NetbootError(ValueError):
    """A fixed diagnostic code; no untrusted command-line or HTTP output."""


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


class NetbootOps(LinuxOps):
    """`LinuxOps` plus network bring-up and console-diagnostic reads.

    Deliberately does NOT add or override `time_ready`, `device_id`,
    `boot_id`, or `arm_trial_watchdog` -- this path never enrolls, never
    verifies a signature, and never arms the trial watchdog, so those steps
    are simply absent rather than stubbed. `mount_root`, `_prepare_root`,
    `ram`, and `command` are inherited from `LinuxOps` unchanged.
    """

    def configure_networking(self) -> None:
        # Reproduces the existing ticketed boot's wrapper script step
        # (appliance/netboot_initramfs/scripts/photowall-netboot's
        # configure_networking, an initramfs-tools shell helper) by shelling
        # out to the same helper, rather than reimplementing DHCP/`ip=`
        # parsing here.
        self.command("sh", "-c", ". /scripts/functions 2>/dev/null; configure_networking",
                     timeout=60)

    def network_info(self) -> dict[str, str]:
        """Best-effort acquired-IP / gateway / DNS-server / search-domain
        summary for the phase-3 console log. Never raises: diagnostics must
        not fail the boot."""
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
            info["ip"] = socket.gethostbyname(socket.gethostname())
        except OSError:
            pass
        try:
            nameservers: list[str] = []
            search: list[str] = []
            with open("/etc/resolv.conf") as handle:
                for line in handle:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0] == "nameserver":
                        nameservers.append(parts[1])
                    elif len(parts) >= 2 and parts[0] in ("search", "domain"):
                        search.extend(parts[1:])
            if nameservers:
                info["dns"] = ",".join(nameservers)
            if search:
                info["search"] = ",".join(search)
        except OSError:
            pass
        return info

    def resolve(self, host: str) -> list[str]:
        """Resolve Central's hostname to IPs for the phase-4 console log -- the
        single most common field failure. Raises `NetbootError` on failure so
        the boot fails loudly at the DNS step rather than deep in the HTTP
        client (fail-closed reboot; the appliance retries)."""
        try:
            results = socket.getaddrinfo(host, None)
        except (OSError, UnicodeError):
            raise NetbootError("netboot_dns") from None
        addresses = sorted({item[4][0] for item in results})
        if not addresses:
            raise NetbootError("netboot_dns")
        return addresses


def read_cmdline(path: Path) -> str:
    # Procfs pseudo-files report st_size=0 (see bootstrap.py boot_id(), which
    # reads /proc/sys/kernel/random/boot_id the same way, not via
    # bootstrap.read_regular, for exactly this reason); bound the read
    # explicitly instead.
    with path.open("rb") as stream:
        return stream.read(64 * 1024).decode()


def parse_cmdline(text: str) -> dict[str, str]:
    """`key=value` kernel command-line tokens; bare flags are ignored and a
    repeated key keeps its first value, mirroring the kernel's own
    left-to-right precedence."""
    result: dict[str, str] = {}
    for token in text.split():
        key, sep, value = token.partition("=")
        if sep and key not in result:
            result[key] = value
    return result


def _is_truthy(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() not in ("", "0", "false", "no", "off")


def _validate_central_root(url: str) -> str:
    """Validate `photowall.central` (Central's ROOT URL) and return its
    `AppFetcher` origin (`scheme://netloc`).

    Keeps the hardening the former `photowall.base_url` validator carried
    (mirrors `bootstrap.BootConfig`'s origin validation) -- http/https only,
    a hostname, no userinfo, no query/fragment, no backslashes, a length cap,
    no control characters -- but this is a ROOT, so the path must be empty or
    `/`: the concrete netboot path is `NETBOOT_BASE_PATH`, appended in code.
    `http` is permitted because this path has no signature to protect
    regardless of transport (0009 Phase-4 owner decision: the base squashfs
    moves over HTTP on the trusted LAN)."""
    try:
        parts = urlsplit(url)
        valid = (parts.scheme in ("http", "https") and bool(parts.hostname)
                  and parts.port != 0 and not parts.username and not parts.password
                  and parts.path in ("", "/") and not parts.query and not parts.fragment
                  and "\\" not in url and len(url) <= 2048
                  and not any(ord(character) <= 32 for character in url))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise NetbootError("netboot_configuration")
    return f"{parts.scheme}://{parts.netloc}"


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
    now arrives in the HTTP `Digest` response header, which the `AppFetcher`
    surfaces only once the response opens -- so it is resolved AFTER the body
    has streamed. A None result fails closed: this path exists to catch
    corruption, so a base served without a usable `Digest` header is refused,
    not mounted blindly.

    Same fail-closed discipline as `bootstrap.copy_verified` -- exclusive
    create, per-block bound, delete any partial file on any failure -- restated
    rather than called directly because `copy_verified` requires a
    `contracts.release.Release` (a signed-ticket `rootfs_size` + `rootfs_sha256`
    pair) that does not exist on this unsigned, ticketless path."""
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
                    log.info(f"phase 7/9 download: {total // (1024 * 1024)} MiB")
            if not total:
                raise NetbootError("netboot_empty")
            expected = expected_digest() if callable(expected_digest) else expected_digest
            computed = digest.hexdigest()
            if log is not None:
                log.info(f"phase 8/9 hash compare: expected={expected} computed={computed}")
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


def netboot(cmdline: Mapping[str, str], rootmnt: Path, *, ops=None,
            fetcher_factory=AppFetcher, serial_reader=read_pi_serial, log=None) -> None:
    """Fetch + RAM-overlay-mount the unsigned rpi-image-gen base from Central,
    self-identifying by serial. Writes no boot-context file (module docstring,
    step 7). On any failure, logs the phase + exception to the console and (if
    `photowall.debug`) pauses before re-raising -- never a shell, always the
    fail-closed reboot the shell wrapper enforces."""
    debug = _is_truthy(cmdline.get("photowall.debug"))
    if log is None:
        log = ConsoleLog(debug=debug)
    try:
        _run_netboot(cmdline, rootmnt, ops, fetcher_factory, serial_reader, log)
    except BaseException as error:
        log.info(f"FAILED: {error.__class__.__name__}: {error}")
        _pre_reboot_pause(debug, log)
        raise


def _run_netboot(cmdline, rootmnt, ops, fetcher_factory, serial_reader, log) -> None:
    # Phase 1: cmdline parsed -- show central + all photowall.* params.
    central = cmdline.get("photowall.central")
    photowall_params = {key: value for key, value in cmdline.items()
                        if key.startswith("photowall.")}
    log.info(f"phase 1/9 cmdline parsed: photowall.central={central!r} params={photowall_params}")
    if not isinstance(central, str) or not central:
        raise NetbootError("netboot_configuration")
    origin = _validate_central_root(central)
    ops = ops or NetbootOps()

    # Phase 2: serial read (self-supplied identity; nothing baked).
    serial = serial_reader()
    if serial is None:
        log.info("phase 2/9 serial: UNAVAILABLE (no firmware serial-number; "
                 "Central serves the default base)")
    else:
        log.info(f"phase 2/9 serial: {serial}")

    # Phase 3: networking up -- acquired IP, gateway, DNS server(s), search.
    ops.configure_networking()
    info = ops.network_info()
    log.info(f"phase 3/9 networking up: ip={info.get('ip', '?')} "
             f"gateway={info.get('gateway', '?')} interface={info.get('interface', '?')} "
             f"dns={info.get('dns', '?')} search={info.get('search', '?')}")

    # Phase 4: DNS resolution of Central's host (the #1 field failure -- loud).
    host = urlsplit(origin).hostname
    addresses = ops.resolve(host)
    log.info(f"phase 4/9 DNS: {host} -> {','.join(addresses)}")

    # Phase 5: HTTP request -- method, full URL, headers sent.
    url = origin + NETBOOT_BASE_PATH
    headers = {SERIAL_HEADER: serial} if serial else {}
    log.info(f"phase 5/9 GET {url} headers={headers}")

    fetcher = fetcher_factory(origin, seconds=BASE_FETCH_SECONDS)
    ram = ops.ram()
    image = ram / RAM_IMAGE_NAME
    captured: dict[str, str | None] = {}

    def on_response(response_headers) -> None:
        # Phase 6: response -- status (200 by the time this fires), Digest,
        # Content-Length. The expected digest is captured here (the header is
        # available only once the response opens) for fetch_verified to resolve
        # after streaming.
        raw_digest = response_headers.get("Digest")
        length = response_headers.get("Content-Length")
        captured["digest"] = parse_digest_header(raw_digest)
        log.info(f"phase 6/9 response: 200 Digest={raw_digest!r} Content-Length={length!r}")
        if captured["digest"] is None:
            log.info("phase 6/9 response: NO usable sha-256 Digest header -- "
                     "fetch will fail closed")

    try:
        chunks = fetcher.chunks(NETBOOT_BASE_PATH, MAX_ROOTFS_BYTES,
                                headers=headers, on_response=on_response)
        fetch_verified(chunks, image, lambda: captured.get("digest"), log=log)
        log.info("phase 9/9 mount + handoff: mounting squashfs")
        ops.mount_root(image, rootmnt)
        log.info("phase 9/9 mount + handoff: success")
    except BaseException:
        image.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rootmnt", type=Path, default=Path("/root"))
    parser.add_argument("--cmdline", type=Path, default=Path("/proc/cmdline"))
    args = parser.parse_args()
    try:
        netboot(parse_cmdline(read_cmdline(args.cmdline)), args.rootmnt)
    except (NetbootError, ProvisionError, OSError, BootstrapFatal):
        raise SystemExit("photo-wall: netboot_failed") from None


if __name__ == "__main__":
    main()
