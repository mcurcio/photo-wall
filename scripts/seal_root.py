#!/usr/bin/env python3
"""The seal of one release root (decision 0019): the manifest the Node verifies.

Run by debian-packaging/seal-hook.sh, last, over a root mmdebstrap built from the snapshot pin
and the local repo:

    python3 -I -B scripts/seal_root.py --root ROOT --role app|manager-primary --deb DEB \
        --abi BASE_ABI_JSON DISPLAY_ABI_JSON --output DIR

It writes the role's entry (`ENTRY_POINT`, the one program the Node runs from the root), strips
every extended attribute, sets every directory to the format's mode, and writes into DIR, beside the root (DIR/rootfs), the
three metadata files appliance/apps/environment.py's release check reads: environment.json
(the reference less its digest and size, the inventory of every file and link, the capacity),
dependency-lock.json (every installed package, version and architecture, as dpkg records them in
the root) and sources.json (the apt sources the root was resolved from: the snapshot pin). It
also writes DIR/../reference.json, the reference alone, for the release writer
(scripts/node_release_writer.py), which adds the image's digest and size.

The reference names the root package's .deb in the local repo (DEB, its sha256, Package and
Version: `ROLES`) and the ABI the repo's photo-wall-node and photo-wall-node-display write
(abi.json). It changes no package file. Build tooling: stdlib and first-party stdlib-only
modules, run on the roots container's python3.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from appliance.apps.environment import (  # noqa: E402
    FORMAT,
    MANIFEST,
    canonical_bytes,
    capacity,
    file_sha256,
    inventory,
    normalize_rootfs_directories,
)
from contracts.app_environment import AppEnvironmentRefV2  # noqa: E402
from contracts.node_boot import APP_PACKAGE, MANAGER_PACKAGE  # noqa: E402

# Each role -> its root package (contracts.node_boot) and what its entry runs.
ROLES: Final = {
    "app": (APP_PACKAGE,
            "/usr/bin/python3 -I -B /usr/lib/photo-wall/player --config /etc/photo-wall/public.json"),
    "manager-primary": (MANAGER_PACKAGE, "/usr/bin/python3 -I -B /usr/lib/photo-wall/app-manager"),
}
# The program the Node runs from a root, whatever its role.
ENTRY_POINT: Final = "/usr/lib/photo-wall-environment/entry"
SOURCES: Final = "etc/apt/sources.list.d"
LOCK: Final = "dependency-lock.json"
SOURCES_FILE: Final = "sources.json"
REFERENCE: Final = "reference.json"


class SealError(Exception):
    """A root or input the seal refuses."""


def _deb_fields(deb: Path) -> tuple[str, str]:
    fields = subprocess.run(["dpkg-deb", "--field", str(deb), "Package", "Version"],
                            capture_output=True, text=True, check=True).stdout
    values = dict(line.partition(": ")[::2] for line in fields.splitlines())
    return values["Package"], values["Version"]


def package_lock(root: Path) -> list[list[str]]:
    """Every package dpkg records as installed in `root`: [package, version, architecture]."""
    listing = subprocess.run(
        ["dpkg-query", f"--admindir={root / 'var/lib/dpkg'}", "-W",
         "-f=${db:Status-Abbrev}\t${binary:Package}\t${Version}\t${Architecture}\n"],
        capture_output=True, text=True, check=True).stdout
    rows = [line.split("\t") for line in listing.splitlines()]
    if any(len(row) != 4 for row in rows):
        raise SealError("seal_package_lock")
    return sorted([name, version, architecture] for status, name, version, architecture in rows
                  if status.strip() == "ii")


def sources(root: Path) -> list[str]:
    """The root's apt source lines, in file order: the snapshot pin it was resolved from."""
    lines = []
    for path in sorted((root / SOURCES).glob("*.list")):
        lines += [line for line in path.read_text().splitlines()
                  if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        raise SealError("seal_sources_missing")
    return lines


def strip_xattrs(root: Path) -> None:
    """Remove every extended attribute under `root`: the image stores none (mksquashfs
    -no-xattrs) and the inventory refuses one, and a file capability (gst-ptp-helper's
    cap_net_bind_service) is a privilege like the setid bits the hook strips."""
    for path in (root, *root.rglob("*")):
        for name in os.listxattr(path, follow_symlinks=False):
            os.removexattr(path, name, follow_symlinks=False)


def seal(root: Path, *, role: str, deb: Path, base_abi: dict, display_abi: dict,
         output: Path) -> AppEnvironmentRefV2:
    """Write the entry, normalise the root's directories and write the metadata into `output`
    (whose rootfs/ is `root`) and the reference beside it. Returns the reference, its digest and
    size placeholders unset (the image's are the release writer's)."""
    package, command = ROLES[role]
    name, version = _deb_fields(deb)
    if name != package:
        raise SealError("seal_root_package_mismatch")
    entry = root / ENTRY_POINT.lstrip("/")
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(f"#!/bin/sh\nexec {command}\n")
    entry.chmod(0o755)
    lock = package_lock(root)
    if [name, version] not in [row[:2] for row in lock]:
        raise SealError("seal_root_package_not_installed")
    architectures = {row[2] for row in lock if row[0] == "dpkg"}
    if len(architectures) != 1:
        raise SealError("seal_root_architecture")
    strip_xattrs(root)
    normalize_rootfs_directories(root)
    lock_bytes = canonical_bytes({"schema": 2, "packages": lock})
    source_bytes = canonical_bytes({"schema": 3, "sources": sources(root)})
    reference = AppEnvironmentRefV2(
        "0" * 64, 1, file_sha256(deb), name, version, architectures.pop(),
        hashlib.sha256(lock_bytes).hexdigest(), hashlib.sha256(source_bytes).hexdigest(), ENTRY_POINT,
        base_abi["base_abi"], display_abi["graphics_abi"], display_abi["plugin_abi"])
    metadata = asdict(reference)
    metadata.pop("environment_sha256")
    metadata.pop("size_bytes")
    files = inventory(root)
    (output / LOCK).write_bytes(lock_bytes)
    (output / SOURCES_FILE).write_bytes(source_bytes)
    (output / MANIFEST).write_bytes(canonical_bytes(
        {"schema": 2, "format": FORMAT, "reference": metadata, "files": files,
         "capacity": capacity(files)}))
    for each in (LOCK, SOURCES_FILE, MANIFEST):
        (output / each).chmod(0o644)
    output.chmod(0o755)
    (output.parent / REFERENCE).write_bytes(canonical_bytes(metadata))
    return reference


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--role", choices=tuple(ROLES), required=True)
    parser.add_argument("--deb", type=Path, required=True)
    parser.add_argument("--abi", type=Path, nargs=2, required=True,
                        metavar=("BASE_ABI_JSON", "DISPLAY_ABI_JSON"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.root.resolve() != (args.output / "rootfs").resolve():
        parser.error("--root must be --output's rootfs/")
    try:
        seal(args.root, role=args.role, deb=args.deb,
             base_abi=json.loads(args.abi[0].read_text()),
             display_abi=json.loads(args.abi[1].read_text()), output=args.output)
    except SealError as error:
        print(f"seal-root: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
