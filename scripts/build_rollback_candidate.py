"""Prepare a private, signed-later rollback candidate from a prepared root.

This helper deliberately does not sign or publish anything.  It is intended for
the CI rollback fixture while the prepared root is still available to the
builder.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

from appliance.build import (
    MAX_ROOTFS_BYTES,
    BuildError,
    boot_abi,
    canonical,
    checked_file,
    manifest,
    outside_git,
    squash,
)

MAX_CANDIDATE_BYTES = 64 * 1024
FAULT_PATH = "etc/systemd/system/photo-wall-player.service.d/99-ci-failure.conf"
FAULT_CONTENT = b"[Service]\nExecStart=\nExecStart=/usr/bin/false\n"
PUBLIC_NAMES = {"public.json", "bootstrap.json", "ca.pem", "release.pub.pem"}


def _regular_json(path: Path, maximum: int) -> tuple[bytes, dict]:
    if path.is_symlink():
        raise BuildError("candidate_symlink")
    try:
        checked_file(path, maximum)
        payload = path.read_bytes()
    except (OSError, BuildError):
        raise BuildError("candidate_json") from None
    if not 0 < len(payload) <= maximum:
        raise BuildError("candidate_limit")
    try:
        value = json.loads(payload)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise BuildError("candidate_json") from None
    if not isinstance(value, dict) or payload != canonical(value):
        raise BuildError("candidate_noncanonical")
    return payload, value


def _reject_symlink_parents(path: Path) -> None:
    """Reject an existing symlink at every component of an absolute path."""
    path = Path(path.absolute())
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise BuildError("candidate_symlink")


def _directory(path: Path, error: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise BuildError(error)
    _reject_symlink_parents(path)


def _configuration(directory: Path, error: str, *, extras: set[str] = frozenset()) -> None:
    """Validate the standard public-input file set; not bound into the signed release."""
    _directory(directory, error)
    entries = set()
    for path in directory.iterdir():
        entries.add(path.name)
        if len(entries) > len(PUBLIC_NAMES) + len(extras):
            raise BuildError(error)
    if entries != PUBLIC_NAMES | extras:
        raise BuildError(error)
    if any(path.is_symlink() for path in directory.iterdir()):
        raise BuildError(error)
    for name in sorted(PUBLIC_NAMES):
        path = directory / name
        if path.is_symlink():
            raise BuildError(error)
        try:
            checked_file(path, 1024**2)
            size = path.stat().st_size
        except (OSError, BuildError):
            raise BuildError(error) from None
        if not 0 < size <= 1024**2:
            raise BuildError(error)


def _read_base(base_bundle: Path) -> tuple[dict, object, Path]:
    _directory(base_bundle, "base_bundle_invalid")
    _, build = _regular_json(base_bundle / "build.json", 4 * 1024**2)
    from contracts.release import Release

    release_payload, _ = _regular_json(base_bundle / "release.json", 8192)
    try:
        release = Release.decode(release_payload)
    except ValueError:
        raise BuildError("base_release_invalid") from None
    epoch = build.get("source_epoch")
    if (build.get("schema") != 1 or build.get("revision") != release.revision
            or type(epoch) is not int
            or build.get("boot_abi") != release.boot_abi
            or build.get("rootfs_sha256") != release.rootfs_sha256
            or build.get("rootfs_size") != release.rootfs_size):
        raise BuildError("base_identity_mismatch")
    if type(epoch) is not int or not 0 < epoch < 2**31:
        raise BuildError("source_epoch_invalid")
    rootfs = base_bundle / release.rootfs_name
    checked_file(rootfs, MAX_ROOTFS_BYTES, expected=release.rootfs_sha256)
    if not (rootfs.stat().st_size == release.rootfs_size):
        raise BuildError("base_rootfs_size")
    boot = base_bundle / "boot"
    _directory(boot, "base_boot_invalid")
    _configuration(base_bundle / "public", "base_public_invalid")
    return build, release, boot


def _kernel_name(root: Path) -> str:
    modules = root / "usr/lib/modules"
    _directory(modules, "root_modules_invalid")
    names = sorted(path.name for path in modules.iterdir()
                   if path.is_dir() and not path.is_symlink())
    if len(names) != 1:
        raise BuildError("root_kernel_ambiguous")
    return names[0]


def _write_exclusive(path: Path, payload: bytes, mode: int = 0o644) -> None:
    created = False
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    created = True
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise BuildError("candidate_write")
            offset += written
        os.fsync(fd)
    except BaseException:
        if created:
            Path(path).unlink(missing_ok=True)
        raise
    finally:
        os.close(fd)


def prepare(root: Path, base_bundle: Path, destination: Path) -> dict:
    """Create a private rollback candidate from a prepared configured root."""
    root = Path(root)
    base_bundle = Path(base_bundle)
    destination = Path(destination)
    if not destination.is_absolute():
        raise BuildError("destination_absolute")
    _directory(root, "root_invalid")
    outside_git(destination)
    _reject_symlink_parents(destination.parent)
    if destination.exists() or destination.is_symlink():
        raise BuildError("output_exists")
    root_abs, base_abs = root.absolute().resolve(), base_bundle.absolute().resolve()
    destination_abs = destination.absolute().resolve()
    if destination_abs == root_abs or destination_abs.is_relative_to(root_abs):
        raise BuildError("destination_inside_root")
    if destination_abs == base_abs or destination_abs.is_relative_to(base_abs):
        raise BuildError("destination_inside_bundle")
    if not destination.parent.is_dir() or destination.parent.is_symlink():
        raise BuildError("destination_parent_invalid")

    build, base, boot = _read_base(base_bundle)
    _configuration(root / "etc/photo-wall", "root_configuration_invalid",
                    extras={"boot-policy.json"})
    _, source = _regular_json(root / "usr/share/photo-wall/build/source.json", 4 * 1024**2)
    if (source.get("revision") != base.revision
            or source.get("source_epoch") != build["source_epoch"]):
        raise BuildError("root_source_mismatch")
    kernel = _kernel_name(root)
    calculated_abi, _ = boot_abi(root, boot, kernel)
    if calculated_abi != base.boot_abi or calculated_abi != build["boot_abi"]:
        raise BuildError("base_boot_abi_mismatch")

    dropin = root / FAULT_PATH
    dropdir = dropin.parent
    # The helper must never overwrite a service override supplied by the image.
    for path in (dropdir, dropin):
        if path.exists() or path.is_symlink():
            raise BuildError("fault_path_exists")
    _reject_symlink_parents(dropdir.parent)

    destination.mkdir(mode=0o700)
    made_dropdir = False
    made_dropin = False
    try:
        dropdir.mkdir(mode=0o755)
        made_dropdir = True
        try:
            _write_exclusive(dropin, FAULT_CONTENT)
        except BaseException:
            # O_EXCL means a collision never belongs to this invocation.
            raise
        else:
            made_dropin = True

        temporary_rootfs = destination / "rootfs.squashfs"
        squash(root, temporary_rootfs, build["source_epoch"])
        candidate = manifest(temporary_rootfs, revision=base.revision,
                             boot_abi=calculated_abi)
        if candidate.rootfs_sha256 == base.rootfs_sha256:
            raise BuildError("candidate_unchanged")
        candidate_rootfs = destination / candidate.rootfs_name
        temporary_rootfs.rename(candidate_rootfs)
        _write_exclusive(destination / "release.json", candidate.encode())
        fault_hash = hashlib.sha256(FAULT_CONTENT).hexdigest()
        metadata = {
            "schema": 1,
            "kind": "ci-rollback-candidate",
            "source_revision": base.revision,
            "source_epoch": build["source_epoch"],
            "boot_abi": calculated_abi,
            "fault_path": FAULT_PATH,
            "content_sha256": fault_hash,
            "releases": {
                "accepted": {"release_id": base.release_id,
                      "rootfs_sha256": base.rootfs_sha256,
                      "rootfs_size": base.rootfs_size},
                "candidate": {"release_id": candidate.release_id,
                      "rootfs_sha256": candidate.rootfs_sha256,
                      "rootfs_size": candidate.rootfs_size},
            },
        }
        encoded = canonical(metadata)
        if len(encoded) > MAX_CANDIDATE_BYTES:
            raise BuildError("candidate_metadata_limit")
        _write_exclusive(destination / "candidate.json", encoded)
        return metadata
    except BaseException:
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        raise
    finally:
        if made_dropin:
            dropin.unlink(missing_ok=True)
        if made_dropdir:
            try:
                dropdir.rmdir()
            except OSError:
                # A concurrent collision is never ours to remove.
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("base_bundle", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.root, args.base_bundle, args.destination),
                     sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
