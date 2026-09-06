"""Verify one cached Player photo blob through a stopped VM overlay.

This test-only probe opens the qcow2 overlay read-only through libguestfs.  It
returns only the requested blob's public size and digest; it never exports the
blob bytes or writes the guest filesystem.
"""

from __future__ import annotations

import argparse
import json
import re
import stat
from pathlib import Path

MAX_OVERLAY_BYTES = 8 * 1024**3
MAX_PHOTO_BYTES = 32 * 1024**2
MAX_MARKER_BYTES = 256
STATE_MARKER_NAME = ".photo-wall-state-v1"


class CacheEvidenceError(ValueError):
    """A fixed, bounded diagnostic code safe for public CI output."""


def _fail(code: str):
    raise CacheEvidenceError(code)


def _overlay(path: Path) -> Path:
    if (not isinstance(path, Path) or not path.is_absolute()
            or path.is_symlink()):
        _fail("invalid_overlay")
    try:
        info = path.lstat()
    except OSError:
        _fail("invalid_overlay")
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or not 0 < info.st_size <= MAX_OVERLAY_BYTES):
        _fail("invalid_overlay")
    return path


def _arguments(sha256: str, size: int, state_marker: bytes) -> None:
    if (not isinstance(sha256, str)
            or re.fullmatch(r"[a-f0-9]{64}", sha256) is None):
        _fail("invalid_sha256")
    if type(size) is not int or not 0 < size <= MAX_PHOTO_BYTES:
        _fail("invalid_photo_size")
    if (not isinstance(state_marker, bytes)
            or not 0 < len(state_marker) <= MAX_MARKER_BYTES):
        _fail("invalid_state_marker")


def _entry(guest, path: str) -> dict:
    try:
        value = guest.lstatns(path)
    except Exception:
        _fail("guest_path_unreadable")
    if not isinstance(value, dict) or type(value.get("st_mode")) is not int:
        _fail("guest_path_invalid")
    return value


def _directory(guest, path: str) -> dict:
    value = _entry(guest, path)
    if not stat.S_ISDIR(value["st_mode"]):
        _fail("guest_path_not_directory")
    return value


def _regular(guest, path: str) -> dict:
    value = _entry(guest, path)
    if not stat.S_ISREG(value["st_mode"]):
        _fail("guest_path_not_regular")
    return value


def _guest_factory(factory):
    if factory is not None:
        return factory
    try:
        import guestfs
    except Exception:
        _fail("guestfs_unavailable")
    return guestfs.GuestFS


def inspect_cache(overlay: Path, sha256: str, size: int, state_marker: bytes, *,
                  guestfs_factory=None) -> dict:
    """Verify one fixed cache path and return bounded public evidence.

    ``state_marker`` must be supplied by the caller from the production
    bootstrap contract.  The probe only checks that exact bytes in the guest
    state filesystem; it does not embed or infer the production marker.
    """
    _arguments(sha256, size, state_marker)
    path = _overlay(overlay)
    guest = None
    successful = False
    try:
        factory = _guest_factory(guestfs_factory)
        guest = factory(python_return_dict=True)
        guest.set_network(False)
        guest.add_drive_opts(str(path), readonly=True, format="qcow2")
        guest.launch()
        filesystems = guest.list_filesystems()
        if not isinstance(filesystems, dict):
            _fail("guest_filesystems_invalid")
        candidates = []
        for device, kind in filesystems.items():
            if kind == "ext4":
                try:
                    label = guest.vfs_label(device)
                except Exception:
                    _fail("guest_filesystem_unreadable")
                if label == "PWSTATE":
                    candidates.append(device)
        if len(candidates) != 1:
            _fail("state_filesystem_ambiguous")
        guest.mount_options("ro,noload,nodev,nosuid,noexec", candidates[0], "/")

        marker = _regular(guest, "/" + STATE_MARKER_NAME)
        if (marker.get("st_uid") != 0 or marker.get("st_gid") != 0
                or marker.get("st_nlink") != 1
                or stat.S_IMODE(marker["st_mode"]) & 0o222
                or marker.get("st_size") != len(state_marker)):
            _fail("state_marker_invalid")
        try:
            actual_marker = guest.read_file("/" + STATE_MARKER_NAME)
        except Exception:
            _fail("state_marker_unreadable")
        if actual_marker != state_marker:
            _fail("state_marker_mismatch")

        _directory(guest, "/player")
        _directory(guest, "/player/cache")
        blob_path = "/player/cache/" + sha256 + ".blob"
        blob = _regular(guest, blob_path)
        if blob.get("st_nlink") != 1 or blob.get("st_size") != size:
            _fail("cache_blob_size_mismatch")
        try:
            digest = guest.checksum("sha256", blob_path)
        except Exception:
            _fail("cache_blob_unreadable")
        if digest != sha256:
            _fail("cache_blob_hash_mismatch")
        successful = True
        return dict(schema=1, sha256=sha256, size=size, verified=True)
    except CacheEvidenceError:
        raise
    except Exception:
        _fail("guestfs_failed")
    finally:
        if guest is not None:
            try:
                guest.close()
            except Exception:
                if successful:
                    _fail("guestfs_close_failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--overlay", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--size", required=True, type=int)
    parser.add_argument("--state-marker-hex", required=True)
    args = parser.parse_args()
    try:
        try:
            marker = bytes.fromhex(args.state_marker_hex)
        except (TypeError, ValueError):
            _fail("invalid_state_marker")
        result = inspect_cache(args.overlay, args.sha256, args.size, marker)
    except CacheEvidenceError as error:
        print(json.dumps({"error": str(error)}, sort_keys=True, separators=(",", ":")))
        raise SystemExit(1) from None
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
