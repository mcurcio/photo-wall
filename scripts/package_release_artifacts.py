"""Package one base-image.yml build's assets into operator-consumable release files.

Pure packaging: it does not build, sign, or verify anything beyond corruption
checks -- it only turns the outputs of `.github/workflows/base-image.yml` (the
netboot base bundle assembled by `scripts/build_netboot_bundle.sh`, plus the
Player and bootstrapper `.deb`s built by `scripts/build_player_deb.py` and
`scripts/build_bootstrapper_deb.py`) into the flat file set
`.github/workflows/release.yml` publishes as a GitHub Release:

- `photo-wall-base-<revision>.tar.gz`: the operator-stageable netboot bundle
  (kernel + initrd + Pi 5 DTBs + the base squashfs + the bundle's own
  `SHA256SUMS`). The operator stages its contents beneath their TFTP boot-server
  tree (`docs/module-pxe-service.md`); every diskless Player netboots it.
- `photo-wall-player_<version>_arm64.deb`: the Player application, copied
  through byte-for-byte under its own build-assigned filename. Central serves
  this by reference (`docs/runbook.md`); it is not installed by the base image.
- `photo-wall-bootstrapper_<version>_arm64.deb`: the bootstrapper package,
  copied through byte-for-byte. It is already baked into the base bundle's
  squashfs; it is published standalone for operators who rebuild the base.
- A top-level `manifest.json` (schema, revision, and each asset's filename +
  sha256 + size) and a `SHA256SUMS` covering every produced file.

Per the home-LAN, no-threat-model ruling (UX over security), NONE of this is
signed: every sha256 here is a **corruption check only** -- proof the bytes were
not truncated or mangled in transit, never an authenticity anchor. There is no
signing key and no `release.pub.pem` anywhere in this module.

Fails closed (`PackagingError`) if any bundle input or `.deb` is missing or
malformed, rather than silently publishing a partial release.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
from pathlib import Path

MIB = 1024**2

MAX_SQUASHFS_BYTES = 2 * 1024**3
MAX_SUMS_BYTES = 4 * 1024**2
MAX_DEB_BYTES = 1 * 1024**3
MAX_TARBALL_BYTES = 4 * 1024**3

# `<name>_<version>_<arch>.deb`, the standard Debian package filename shape the
# `.deb` builders produce. Best-effort only: a `.deb` whose name does not match
# this still packages fine, just without a parsed `version` field in the
# manifest (informational, not load-bearing).
_DEB_FILENAME = re.compile(r"^[a-z0-9.+-]+_(?P<version>[A-Za-z0-9.+~-]+)_[a-z0-9]+\.deb$")


class PackagingError(ValueError):
    """A required build output is missing or malformed for packaging."""


def _outside_git(path: Path) -> None:
    """Refuse to write the release set anywhere inside a Git working tree."""
    resolved = path.absolute().resolve()
    if any((parent / ".git").exists() for parent in (resolved, *resolved.parents)):
        raise PackagingError("artifact_inside_git")


def _checked_file(path: Path, maximum: int) -> dict:
    """Hash one stable regular file without following its leaf symlink.

    A local, disk-image-free copy of the small primitive the retired
    `appliance/build.py` provided; corruption-check only, never a trust anchor.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum:
            raise PackagingError("artifact_limit")
        total, digest = 0, hashlib.sha256()
        while block := os.read(fd, MIB):
            total += len(block)
            if total > maximum:
                raise PackagingError("artifact_limit")
            digest.update(block)
        after = os.fstat(fd)
        if (total != before.st_size or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns):
            raise PackagingError("artifact_changed")
        return {"sha256": digest.hexdigest(), "size": total}
    finally:
        os.close(fd)


def _require_file(path: Path, maximum: int, *, label: str) -> dict:
    if not path.is_file() or path.is_symlink():
        raise PackagingError(f"{label}_missing")
    return _checked_file(path, maximum)


def _deb_record(path: Path, *, label: str) -> dict:
    if path.suffix != ".deb":
        raise PackagingError(f"{label}_invalid")
    record = _require_file(path, MAX_DEB_BYTES, label=label)
    match = _DEB_FILENAME.match(path.name)
    return {
        "filename": path.name,
        "version": match["version"] if match else None,
        "sha256": record["sha256"],
        "size": record["size"],
    }


def package(
    base_bundle: Path,
    player_deb: Path,
    bootstrapper_deb: Path,
    destination: Path,
    *,
    revision: str,
) -> dict:
    """Assemble the flat operator artifact set into a new `destination` directory."""
    if (
        not isinstance(revision, str)
        or len(revision) != 40
        or any(c not in "0123456789abcdef" for c in revision)
    ):
        raise PackagingError("revision_invalid")
    base_bundle = base_bundle.absolute()
    player_deb = player_deb.absolute()
    bootstrapper_deb = bootstrapper_deb.absolute()
    destination = destination.absolute()
    _outside_git(destination)
    if destination.exists() or destination.is_symlink():
        raise PackagingError("destination_exists")

    if not base_bundle.is_dir() or base_bundle.is_symlink():
        raise PackagingError("base_bundle_missing")
    squashfs = base_bundle / "photo-wall-base.squashfs"
    _require_file(squashfs, MAX_SQUASHFS_BYTES, label="base_squashfs")
    boot_tree = base_bundle / "boot"
    if not boot_tree.is_dir() or boot_tree.is_symlink() or not any(boot_tree.iterdir()):
        raise PackagingError("base_boot_tree_missing")
    _require_file(base_bundle / "SHA256SUMS", MAX_SUMS_BYTES, label="base_sha256sums")

    player_record = _deb_record(player_deb, label="player_deb")
    bootstrapper_record = _deb_record(bootstrapper_deb, label="bootstrapper_deb")

    destination.mkdir(mode=0o700, parents=True)
    staging = destination / ".staging"
    staging.mkdir(mode=0o700)
    try:
        base_root = staging / "photo-wall-base"
        shutil.copytree(base_bundle, base_root, symlinks=True,
                        ignore=shutil.ignore_patterns(".staging"))
        base_tarball_name = f"photo-wall-base-{revision}.tar.gz"
        base_tarball = destination / base_tarball_name
        with tarfile.open(base_tarball, "w:gz") as archive:
            archive.add(base_root, arcname="photo-wall-base")

        shutil.copyfile(player_deb, destination / player_record["filename"])
        shutil.copyfile(bootstrapper_deb, destination / bootstrapper_record["filename"])
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    base_tarball_record = _checked_file(base_tarball, MAX_TARBALL_BYTES)
    manifest = {
        "schema": 1,
        "revision": revision,
        "base_image": {
            "filename": base_tarball_name,
            "sha256": base_tarball_record["sha256"],
            "size": base_tarball_record["size"],
        },
        "player_deb": player_record,
        "bootstrapper_deb": bootstrapper_record,
    }
    (destination / "manifest.json").write_bytes(
        (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )

    sums = "".join(
        f"{_checked_file(path, MAX_TARBALL_BYTES)['sha256']}  {path.name}\n"
        for path in sorted(path for path in destination.iterdir() if path.is_file())
    )
    (destination / "SHA256SUMS").write_text(sums)

    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-bundle", type=Path, required=True,
                        help="netboot base bundle directory from build_netboot_bundle.sh")
    parser.add_argument("--player-deb", type=Path, required=True,
                        help="the .deb produced by scripts/build_player_deb.py")
    parser.add_argument("--bootstrapper-deb", type=Path, required=True,
                        help="the .deb produced by scripts/build_bootstrapper_deb.py")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    try:
        result = package(
            args.base_bundle, args.player_deb, args.bootstrapper_deb, args.destination,
            revision=args.revision,
        )
    except (PackagingError, OSError) as exc:
        parser.exit(1, f"Release artifact packaging failed: {exc}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
