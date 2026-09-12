"""Ticketless netboot init for the rpi-image-gen base (0009 Phase 4, slice
p4-boot-chain part 1: docs/decisions/0009-minimal-base-and-app-package.md,
"Phase 4 boot-chain + retirement plan").

This is a NEW, additive boot path. It does not touch, replace, or retire
`appliance/bootstrap.py`'s `boot()` (the ticket/signed-rootfs flow) -- that
stays live for the existing netboot tier and its e2e coverage. This module
boots the *unsigned* rpi-image-gen base squashfs instead: no boot ticket, no
signature verification, and no trial watchdog.

What it does, in order:

1. Reads the base-image URL (and an optional corruption sha256) from the
   kernel command line -- `photowall.base_url=` and `photowall.base_sha256=`
   -- set by the operator's boot server, never baked into any image.
2. Brings up networking. The kernel + this initramfs still arrive over TFTP;
   only the (large) base squashfs moves to HTTP (0009 Phase-4 owner
   decision). Network bring-up itself reuses the exact mechanism the
   existing ticketed boot's initramfs wrapper already relies on --
   initramfs-tools' `configure_networking` shell helper
   (`appliance/initramfs/scripts/photowall:11`) -- rather than reimplementing
   DHCP/`ip=` parsing in Python; `NetbootOps.configure_networking` shells out
   to the same helper.
3. Fetches the base squashfs over HTTP using `appliance.provision.AppFetcher`
   (bounded deadline, no proxies/redirects, exact `Content-Length` bound,
   streamed reads -- the same discipline as `appliance.bootstrap.Fetcher`,
   reused here as-is because it already works from a bare origin string with
   no baked HTTPS-only `BootConfig`). If `photowall.base_sha256` is present,
   the downloaded bytes are checked against it as a **corruption check
   only** -- there is no signature anywhere on this path (mirrors 0009's
   ruling for the app `.deb`). Failure (network, size, corruption) is fail
   closed: no partial file is left behind and nothing is mounted.
4. RAM-overlay-mounts the fetched squashfs by calling
   `appliance.bootstrap.LinuxOps.mount_root` **unchanged**
   (`appliance/bootstrap.py:366-399`; `NetbootOps` subclasses `LinuxOps` and
   adds only `configure_networking` -- `mount_root`/`_prepare_root`/`ram`/
   `command` are inherited verbatim, not reimplemented).
5. Writes NO boot-context file at all: no `ticket_id`, no
   `persistence="volatile"`, nothing under `ops.run_root / "boot.json"`.
   `player.service.resolve_boot_context` falls back to
   `hardware_boot_context()` (`ticket_id=None`) whenever that file is
   absent, which is exactly what makes the app enroll ticketless on this
   root (docs/decisions/0009-minimal-base-and-app-package.md:753-758).

Every external effect -- reading the command line, bringing up networking,
the HTTP fetch, and the mount -- is an injected callable/object, so this is
unit-testable with no root, no real network, and no kernel.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from appliance.bootstrap import CHUNK, BootstrapFatal, LinuxOps
from appliance.provision import AppFetcher, ProvisionError
from contracts.release import MAX_ROOTFS_BYTES

RAM_IMAGE_NAME = "photo-wall-base.squashfs"
_SHA256 = re.compile(r"[a-f0-9]{64}")


class NetbootError(ValueError):
    """A fixed diagnostic code; no untrusted command-line or HTTP output."""


class NetbootOps(LinuxOps):
    """`LinuxOps` plus network bring-up.

    Deliberately does NOT add or override `time_ready`, `device_id`,
    `boot_id`, or `arm_trial_watchdog` -- this path never enrolls, never
    verifies a signature, and never arms the trial watchdog, so those steps
    are simply absent rather than stubbed. `mount_root`, `_prepare_root`,
    `ram`, and `command` are inherited from `LinuxOps` unchanged.
    """

    def configure_networking(self) -> None:
        # Reproduces the existing ticketed boot's wrapper script step
        # (appliance/initramfs/scripts/photowall:11 `configure_networking`,
        # an initramfs-tools shell helper run before Python is ever
        # invoked) by shelling out to the same helper, rather than
        # reimplementing DHCP/`ip=` parsing here.
        self.command("sh", "-c", ". /scripts/functions 2>/dev/null; configure_networking",
                     timeout=60)


def read_cmdline(path: Path) -> str:
    # Procfs pseudo-files report st_size=0 (see bootstrap.py:293-296's
    # boot_id(), which reads /proc/sys/kernel/random/boot_id the same way,
    # not via bootstrap.read_regular, for exactly this reason); bound the
    # read explicitly instead.
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


def _split_base_url(url: str) -> tuple[str, str]:
    """Validate `photowall.base_url` and split it into an `AppFetcher`
    origin plus request path. Mirrors `bootstrap.BootConfig`'s origin
    validation (bootstrap.py:98-111) but additionally permits `http`: this
    path has no signature to protect regardless of transport (0009 Phase-4
    owner decision: the base squashfs moves over HTTP)."""
    try:
        parts = urlsplit(url)
        valid = (parts.scheme in ("http", "https") and bool(parts.hostname)
                  and parts.port != 0 and not parts.username and not parts.password
                  and bool(parts.path) and not parts.query and not parts.fragment
                  and "\\" not in url and len(url) <= 2048
                  and not any(ord(character) <= 32 for character in url))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise NetbootError("netboot_configuration")
    return f"{parts.scheme}://{parts.netloc}", parts.path


def fetch_verified(chunks, destination: Path, sha256: str | None) -> None:
    """Stream the base squashfs into `destination`, checking `sha256` (if
    given) as a corruption check only.

    Same fail-closed discipline as `bootstrap.copy_verified`
    (bootstrap.py:227-249) -- exclusive create, per-block bound, delete any
    partial file on any failure -- restated rather than called directly
    because `copy_verified` requires a `contracts.release.Release` (a
    mandatory `rootfs_size` + `rootfs_sha256` pair bound to a signed
    ticket), which does not exist on this unsigned, ticketless path: here
    the sha256 is an *optional* cmdline value and the size is not known in
    advance.
    """
    digest = hashlib.sha256()
    created = False
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
            if not total:
                raise NetbootError("netboot_empty")
            if sha256 is not None and digest.hexdigest() != sha256:
                raise NetbootError("netboot_integrity")
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


def netboot(cmdline: Mapping[str, str], rootmnt: Path, *, ops=None,
            fetcher_factory=AppFetcher) -> None:
    """Fetch + RAM-overlay-mount the unsigned rpi-image-gen base named on
    `cmdline`. Writes no boot-context file (see module docstring, step 5)."""
    ops = ops or NetbootOps()
    base_url = cmdline.get("photowall.base_url")
    if not isinstance(base_url, str) or not base_url:
        raise NetbootError("netboot_configuration")
    origin, path = _split_base_url(base_url)
    base_sha256 = cmdline.get("photowall.base_sha256")
    if base_sha256 is not None and not _SHA256.fullmatch(base_sha256):
        raise NetbootError("netboot_configuration")
    ops.configure_networking()
    fetcher = fetcher_factory(origin)
    ram = ops.ram()
    image = ram / RAM_IMAGE_NAME
    try:
        fetch_verified(fetcher.chunks(path, MAX_ROOTFS_BYTES), image, base_sha256)
        ops.mount_root(image, rootmnt)
    except BaseException:
        image.unlink(missing_ok=True)
        raise


def main() -> None:
    import argparse

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
