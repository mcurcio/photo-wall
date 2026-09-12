#!/usr/bin/env python3
"""Portable content-verify for the p4-boot-chain s2a netboot initrd.

Asserts, against an ``lsinitramfs``-style member listing, that the initrd built
by ``appliance/netboot_initramfs/`` carries exactly the slim UNSIGNED netboot
init and its 7-file closure -- and none of the signed/app-only material that
must never ride this path.

Stdlib-only and importable (``check_listing`` is the unit-tested seam). It is
deliberately NOT part of ``appliance/build.py``: build.py and the old signed
initramfs are slated for p4-retire, and this verify must survive that deletion.

Contract (0008 p4-boot-chain s2a "Init closure"):

POSITIVE -- every one of these must be present:
  * a python3 interpreter (version-agnostic: ``usr/bin/python3*``),
  * the ``_ssl`` / ``_hashlib`` / ``_socket`` lib-dynload extension modules,
  * the boot script ``scripts/photowall-netboot``,
  * initramfs-tools' ``scripts/functions`` (its configure_networking helper,
    which appliance.netboot_init sources -- load-bearing),
  * all 7 closure module files.

NEGATIVE -- none of these may be present:
  * ``appliance/updates.py`` (signed-release verifier, never reached here),
  * any signed/time material (``release.pub.pem``, ``*.sig``, ``ca.pem``,
    ``bootstrap.json``, ``boot-policy.json``),
  * any ``player/`` or ``central/`` code, ``zeroconf`` / ``ifaddr`` (mDNS deps),
  * ``gi/`` / GTK / GStreamer (the app UI stack).
"""

from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys
from pathlib import Path

# Present-or-fail globs, matched against normalised member paths.
REQUIRED_GLOBS: tuple[tuple[str, str], ...] = (
    ("python3 interpreter", "usr/bin/python3*"),
    ("_ssl extension module", "*lib-dynload/_ssl*.so"),
    ("_hashlib extension module", "*lib-dynload/_hashlib*.so"),
    ("_socket extension module", "*lib-dynload/_socket*.so"),
    ("netboot boot script", "scripts/photowall-netboot"),
    ("initramfs-tools configure_networking helper", "scripts/functions"),
)

# The exact 7-file init closure (matched as trailing path components, so the
# python minor version in the staging prefix does not matter).
CLOSURE_MODULES: tuple[str, ...] = (
    "appliance/__init__.py",
    "appliance/bootstrap.py",
    "appliance/provision.py",
    "appliance/netboot_init.py",
    "contracts/__init__.py",
    "contracts/release.py",
    "contracts/equipment.py",
)

# Forbidden-if-present globs.
FORBIDDEN_GLOBS: tuple[tuple[str, str], ...] = (
    ("appliance.updates (signed-release verifier)", "*appliance/updates.py"),
    ("release public key", "*release.pub.pem"),
    ("signature file", "*.sig"),
    ("CA bundle", "*ca.pem"),
    ("signed bootstrap config", "*bootstrap.json"),
    ("signed boot policy", "*boot-policy.json"),
)

# Forbidden whole path components (top-level packages that must not ship).
FORBIDDEN_COMPONENTS: frozenset[str] = frozenset(
    {"player", "central", "zeroconf", "ifaddr", "gi"}
)

# Forbidden case-insensitive substrings (the GTK/GStreamer UI stack).
FORBIDDEN_SUBSTRINGS: tuple[tuple[str, str], ...] = (
    ("GTK library", "libgtk"),
    ("GStreamer library", "libgst"),
    ("GStreamer", "gstreamer"),
)


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


def check_listing(members) -> list[str]:
    """Return the s2a contract violations for ``members`` (empty = pass)."""
    paths = _normalise(members)
    violations: list[str] = []

    for label, pattern in REQUIRED_GLOBS:
        if not any(fnmatch.fnmatch(path, pattern) for path in paths):
            violations.append(f"missing required {label} (pattern {pattern!r})")

    for module in CLOSURE_MODULES:
        suffix = "/" + module
        if not any(path == module or path.endswith(suffix) for path in paths):
            violations.append(f"missing closure module {module}")

    for path in sorted(paths):
        for label, pattern in FORBIDDEN_GLOBS:
            if fnmatch.fnmatch(path, pattern):
                violations.append(f"forbidden {label}: {path}")
        components = set(path.split("/"))
        for component in sorted(FORBIDDEN_COMPONENTS & components):
            violations.append(f"forbidden package {component!r}: {path}")
        lowered = path.lower()
        for label, needle in FORBIDDEN_SUBSTRINGS:
            if needle in lowered:
                violations.append(f"forbidden {label}: {path}")

    return violations


def _lsinitramfs(initrd: Path) -> list[str]:
    result = subprocess.run(
        ["lsinitramfs", str(initrd)],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("initrd", nargs="?", type=Path,
                        help="initrd image to inspect via lsinitramfs")
    parser.add_argument("--listing", type=Path,
                        help="a file holding a pre-captured lsinitramfs listing")
    args = parser.parse_args(argv)

    if args.initrd and args.listing:
        parser.error("give either an INITRD path or --listing, not both")
    if not args.initrd and not args.listing:
        parser.error("give an INITRD path or --listing FILE")

    if args.listing:
        members = args.listing.read_text().splitlines()
    else:
        members = _lsinitramfs(args.initrd)

    violations = check_listing(members)
    for violation in violations:
        print(violation)
    if violations:
        print(f"FAIL: {len(violations)} netboot initrd contract violation(s)")
        return 1
    print("OK: netboot initrd content contract satisfied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
