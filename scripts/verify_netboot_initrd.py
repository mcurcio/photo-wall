#!/usr/bin/env python3
"""Portable content-verify for the netboot initrd (0008 p4-boot-chain s2a; decision 0014 §5).

The shipped `initrd.img` is two archives: the boot data (scripts/build_boot_data.py; stage 1's
computed closure, the CA bundle and the clock floor), uncompressed, in FRONT of the cached
compressed archive `appliance/netboot_initramfs/` builds. This checks both, against the closure
manifest the same build wrote, so neither the module list nor the forbidden-package policy is
restated here.

Stdlib-only and importable (``check_listing`` is the unit-tested seam). It is deliberately NOT
part of ``appliance/build.py``: build.py and the old signed initramfs are slated for p4-retire,
and this verify must survive that deletion.

POSITIVE -- every one of these must be present:
  * in the cached archive: a python3 interpreter (``usr/bin/python3*``), the ``_ssl`` /
    ``_hashlib`` lib-dynload extension modules, the stdlib's ``socket.py`` (on Debian
    ``_socket`` is built into libpython, so the stdlib tree plus ``_ssl.so`` is the honest
    file-level proxy for "the initrd can open TCP and TLS sockets"), the boot script
    ``scripts/photowall-netboot`` and initramfs-tools' ``scripts/functions`` (its
    configure_networking helper, which appliance.netboot_init sources -- load-bearing);
  * in the boot data: the CA bundle, the clock floor, and every closure file under the cached
    interpreter's own stdlib dir, where ``python3 -I`` finds it.

NEGATIVE -- none of these may be present:
  * a boot-data FILE also in the cached archive (the later archive would silently win; shared
    directories are fine);
  * any package the manifest forbids (``INITRD_FORBIDDEN``), as a Python package;
  * signed material (``release.pub.pem``, ``*.sig``, ``ca.pem``, ``bootstrap.json``,
    ``boot-policy.json``) and ``appliance/updates.py``;
  * GTK / GStreamer (the app UI stack).
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.build_boot_data import CA_BUNDLE_PATH, FLOOR_PATH, read_archive  # noqa: E402
from scripts.module_closure import Manifest, read_manifest  # noqa: E402

# The boot script staged verbatim into the initrd (0014 rev 5, design §2.8):
# checked by SOURCE TEXT, not by extracting it back out of a built initrd --
# the build copies it byte-for-byte (appliance/netboot_initramfs/hooks), so
# the repo file IS the shipped content.
DEFAULT_BOOT_SCRIPT = REPO / "appliance" / "netboot_initramfs" / "scripts" / "photowall-netboot"

_PANIC_DEFINITION = re.compile(r"(?m)^\s*panic\s*\(\s*\)\s*\{")
_REBOOT_TOKEN = re.compile(r"(?<![\w-])reboot(?![\w-])")

# Present-or-fail globs for the CACHED archive, matched against normalised member paths.
REQUIRED_GLOBS: tuple[tuple[str, str], ...] = (
    ("python3 interpreter", "usr/bin/python3*"),
    ("_ssl extension module", "*lib-dynload/_ssl*.so"),
    ("_hashlib extension module", "*lib-dynload/_hashlib*.so"),
    # NOT "*lib-dynload/_socket*.so": _socket is a BUILT-IN module in Debian's
    # libpython (no standalone .so). Require the stdlib socket.py instead --
    # its presence proves the stdlib tree landed; _ssl.so covers the C layer.
    ("socket stdlib module", "*lib/python3*/socket.py"),
    ("netboot boot script", "scripts/photowall-netboot"),
    ("initramfs-tools configure_networking helper", "scripts/functions"),
)

# What the boot data must carry besides the closure.
BOOT_DATA_FILES: tuple[tuple[str, str], ...] = (
    ("CA bundle", CA_BUNDLE_PATH),
    ("clock floor", FLOOR_PATH),
)

# Forbidden-if-present globs.
FORBIDDEN_GLOBS: tuple[tuple[str, str], ...] = (
    ("appliance.updates (signed-release verifier)", "*appliance/updates.py"),
    ("release public key", "*release.pub.pem"),
    ("signature file", "*.sig"),
    ("app CA", "*ca.pem"),
    ("signed bootstrap config", "*bootstrap.json"),
    ("signed boot policy", "*boot-policy.json"),
)

# Forbidden case-insensitive substrings (the GTK/GStreamer UI stack).
FORBIDDEN_SUBSTRINGS: tuple[tuple[str, str], ...] = (
    ("GTK library", "libgtk"),
    ("GStreamer library", "libgst"),
    ("GStreamer", "gstreamer"),
)

_STDLIB_SOCKET = "*lib/python3*/socket.py"
_PYTHON_DIR = re.compile(r"python3(\.\d+)?|dist-packages|site-packages")


def _normalise(members) -> set[str]:
    """Accept text or an iterable of member paths; return normalised paths.

    Strips whitespace and any leading ``./`` or ``/`` so an ``lsinitramfs``
    listing (which may print ``./usr/...``, ``/usr/...`` or ``usr/...``)
    compares uniformly.
    """
    if isinstance(members, bytes):
        members = members.decode("utf-8", "replace")
    if isinstance(members, str):
        members = members.splitlines()
    result: set[str] = set()
    for raw in members:
        path = raw.strip()
        while path.startswith("./"):
            path = path[2:]
        path = path.lstrip("/")
        if path:
            result.add(path)
    return result


def _python_package(path: str) -> str | None:
    """The top-level package a path installs, when it sits in a Python library directory
    (``usr/lib/python3.13/<package>/...``, ``.../dist-packages/<package>/...``)."""
    parts = path.split("/")
    libraries = [index for index, part in enumerate(parts[:-1]) if _PYTHON_DIR.fullmatch(part)]
    return parts[libraries[-1] + 1].removesuffix(".py") if libraries else None


def check_listing(early_files: Iterable[str] | str, cached: Iterable[str] | str, *,
                  manifest: Manifest) -> list[str]:
    """Return the contract violations (empty = pass) for the boot data's FILE paths and the
    cached archive's listing, under the closure `manifest` the build wrote."""
    early, cached_paths = _normalise(early_files), _normalise(cached)
    violations: list[str] = []

    for label, pattern in REQUIRED_GLOBS:
        if not any(fnmatch.fnmatch(path, pattern) for path in cached_paths):
            violations.append(f"missing required {label} (pattern {pattern!r})")

    for label, path in BOOT_DATA_FILES:
        if path not in early:
            violations.append(f"missing boot-data {label} ({path})")

    # The closure must sit in the cached interpreter's own stdlib dir, where python3 -I finds it.
    stdlib_dirs = {path.removesuffix("/socket.py") for path in cached_paths
                   if fnmatch.fnmatch(path, _STDLIB_SOCKET)}
    for module in manifest.files:
        if not any(f"{stdlib}/{module}" in early for stdlib in stdlib_dirs):
            violations.append(f"missing closure module {module} (not in the boot data under "
                              f"the initrd interpreter's stdlib dir)")

    for path in sorted(early & cached_paths):
        violations.append(f"boot-data file also in the cached archive (it would win): {path}")

    forbidden = frozenset(manifest.forbidden)
    for path in sorted(early | cached_paths):
        for label, pattern in FORBIDDEN_GLOBS:
            if fnmatch.fnmatch(path, pattern):
                violations.append(f"forbidden {label}: {path}")
        package = _python_package(path)
        if package in forbidden:
            violations.append(f"forbidden package {package!r}: {path}")
        lowered = path.lower()
        for label, needle in FORBIDDEN_SUBSTRINGS:
            if needle in lowered:
                violations.append(f"forbidden {label}: {path}")

    return violations


def check_boot_script(text: str) -> list[str]:
    """Return the s2a liveness contract violations for the boot script's
    SOURCE TEXT (0014 rev 5, design §2.8; empty = pass).

    A `reboot` command runs the kernel's reboot notifier, which stops the
    Pi's watchdog and shuts devices down BEFORE resetting -- the very hang
    this script exists to recover from, with nothing left watching. The
    script must instead (re)define `panic()`, so every later `panic()` call
    -- including one initramfs-tools itself makes after mountroot returns --
    leaves through `photowall_restart`. Full-line `#` comments (this
    function's own explanation of what the script no longer does) are
    stripped first, so documenting the retired behaviour is not itself a
    violation."""
    code = "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))
    violations: list[str] = []
    if _REBOOT_TOKEN.search(code):
        violations.append("boot script contains a 'reboot' command")
    if not _PANIC_DEFINITION.search(code):
        violations.append("boot script does not define panic()")
    return violations


def split_initrd(initrd: Path) -> tuple[list[str], list[str]]:
    """(the boot data's file paths, the cached archive's lsinitramfs listing). ValueError when
    the initrd does not start with the boot data."""
    data = initrd.read_bytes()
    members, end = read_archive(data)
    with tempfile.NamedTemporaryFile(prefix="cached-initrd-") as cached:
        cached.write(data[end:])
        cached.flush()
        listing = subprocess.run(["lsinitramfs", cached.name], capture_output=True, text=True,
                                 check=True).stdout.splitlines()
    return [name for name, is_directory in members if not is_directory], listing


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("initrd", nargs="?", type=Path,
                        help="the initrd image to inspect (boot data + cached archive)")
    parser.add_argument("--manifest", type=Path, required=True,
                        help="the closure-manifest.json build_boot_data.py wrote")
    parser.add_argument("--early-listing", type=Path,
                        help="a file listing the boot data's file paths")
    parser.add_argument("--cached-listing", type=Path,
                        help="a file holding the cached archive's lsinitramfs listing")
    parser.add_argument("--boot-script", type=Path, default=DEFAULT_BOOT_SCRIPT,
                        help="the photowall-netboot source to content-check "
                             "(default: the repo's own copy)")
    args = parser.parse_args(argv)

    listings = (args.early_listing, args.cached_listing)
    if args.initrd and any(listings):
        parser.error("give either an INITRD path or the two listings, not both")
    if not args.initrd and not all(listings):
        parser.error("give an INITRD path or --early-listing FILE --cached-listing FILE")

    if args.initrd:
        try:
            early, cached = split_initrd(args.initrd)
        except ValueError as error:
            print(f"initrd does not start with the boot-data archive: {error}")
            print("FAIL: 1 netboot initrd contract violation(s)")
            return 1
    else:
        early = args.early_listing.read_text().splitlines()
        cached = args.cached_listing.read_text().splitlines()

    violations = check_listing(early, cached, manifest=read_manifest(args.manifest))
    violations += check_boot_script(args.boot_script.read_text())
    for violation in violations:
        print(violation)
    if violations:
        print(f"FAIL: {len(violations)} netboot initrd contract violation(s)")
        return 1
    print("OK: netboot initrd content contract satisfied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
