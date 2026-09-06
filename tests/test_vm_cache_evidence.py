from __future__ import annotations

import hashlib
import json
import stat
import sys
from pathlib import Path

import pytest

from scripts import vm_cache_evidence as evidence

CONTENT = b"synthetic cached photo"
DIGEST = hashlib.sha256(CONTENT).hexdigest()
MARKER = b"photo-wall-state-v1\n"


def _mode(kind: int, permissions: int) -> int:
    return kind | permissions


class FakeGuest:
    def __init__(self, filesystems=None, *, marker=MARKER, entries=None, digest=DIGEST,
                 close_error=None):
        self.filesystems = filesystems if filesystems is not None else {"/dev/sda": "ext4"}
        self.marker = marker
        self.entries = entries or {}
        self.digest = digest
        self.close_error = close_error
        self.calls = []

    def add_drive_opts(self, *args, **kwargs):
        self.calls.append(("add_drive_opts", args, kwargs))

    def set_network(self, enabled):
        self.calls.append(("set_network", enabled))

    def launch(self):
        self.calls.append(("launch",))

    def list_filesystems(self):
        self.calls.append(("list_filesystems",))
        return self.filesystems

    def vfs_label(self, device):
        self.calls.append(("vfs_label", device))
        return "PWSTATE" if self.filesystems.get(device) == "ext4" else None

    def mount_options(self, *args):
        self.calls.append(("mount_options", args))

    def lstatns(self, path):
        self.calls.append(("lstatns", path))
        if path not in self.entries:
            raise RuntimeError("missing path")
        return self.entries[path]

    def read_file(self, path):
        self.calls.append(("read_file", path))
        return self.marker

    def checksum(self, algorithm, path):
        self.calls.append(("checksum", algorithm, path))
        return self.digest

    def close(self):
        self.calls.append(("close",))
        if self.close_error:
            raise RuntimeError(self.close_error)


def valid_guest(**changes):
    entries = {
        "/.photo-wall-state-v1": dict(st_mode=_mode(stat.S_IFREG, 0o444), st_uid=0,
                                       st_gid=0, st_nlink=1, st_size=len(MARKER)),
        "/player": dict(st_mode=_mode(stat.S_IFDIR, 0o700), st_uid=10001,
                         st_gid=10001, st_nlink=1),
        "/player/cache": dict(st_mode=_mode(stat.S_IFDIR, 0o700), st_uid=10001,
                               st_gid=10001, st_nlink=1),
        f"/player/cache/{DIGEST}.blob": dict(st_mode=_mode(stat.S_IFREG, 0o600),
                                              st_uid=10001, st_gid=10001, st_nlink=1,
                                              st_size=len(CONTENT)),
    }
    values = dict(entries=entries)
    values.update(changes)
    if "marker" in changes:
        values["entries"]["/.photo-wall-state-v1"]["st_size"] = len(changes["marker"])
    return FakeGuest(**values)


def overlay(tmp_path: Path) -> Path:
    path = tmp_path / "disk.qcow2"
    path.write_bytes(b"qcow2 fixture")
    return path


def test_inspect_cache_mounts_labeled_state_read_only_and_returns_only_digest(tmp_path):
    guest = valid_guest()
    result = evidence.inspect_cache(overlay(tmp_path), DIGEST, len(CONTENT), MARKER,
                                    guestfs_factory=lambda **kwargs: guest)
    assert result == dict(schema=1, sha256=DIGEST, size=len(CONTENT), verified=True)
    assert all("read_file" not in call[0] or call[1] == "/.photo-wall-state-v1"
               for call in guest.calls)
    assert guest.calls[:4] == [
        ("set_network", False),
        ("add_drive_opts", (str(tmp_path / "disk.qcow2"),),
         {"readonly": True, "format": "qcow2"}),
        ("launch",), ("list_filesystems",),
    ]
    assert guest.calls[4:6] == [("vfs_label", "/dev/sda"),
                                ("mount_options", ("ro,noload,nodev,nosuid,noexec", "/dev/sda", "/"))]
    assert guest.calls[-1] == ("close",)


@pytest.mark.parametrize("filesystems, error", [
    ({}, "state_filesystem_ambiguous"),
    ({"/dev/sda": "ext4", "/dev/sdb": "ext4"}, "state_filesystem_ambiguous"),
])
def test_missing_or_ambiguous_labeled_state_fails_closed_and_closes(tmp_path, filesystems, error):
    guest = valid_guest(filesystems=filesystems)
    with pytest.raises(evidence.CacheEvidenceError, match=error):
        evidence.inspect_cache(overlay(tmp_path), DIGEST, len(CONTENT), MARKER,
                               guestfs_factory=lambda **kwargs: guest)
    assert guest.calls[-1] == ("close",)


def test_corrupt_marker_is_rejected_without_reading_blob(tmp_path):
    guest = valid_guest(marker=b"x" * len(MARKER))
    with pytest.raises(evidence.CacheEvidenceError, match="state_marker_mismatch"):
        evidence.inspect_cache(overlay(tmp_path), DIGEST, len(CONTENT), MARKER,
                               guestfs_factory=lambda **kwargs: guest)
    assert not any(call[0] == "checksum" for call in guest.calls)


@pytest.mark.parametrize("path, mode, error", [
    ("/player", _mode(stat.S_IFLNK, 0o777), "guest_path_not_directory"),
    ("/player/cache", _mode(stat.S_IFLNK, 0o777), "guest_path_not_directory"),
    (f"/player/cache/{DIGEST}.blob", _mode(stat.S_IFLNK, 0o777), "guest_path_not_regular"),
])
def test_symlink_parent_or_blob_is_rejected(tmp_path, path, mode, error):
    guest = valid_guest()
    guest.entries[path] = dict(st_mode=mode, st_uid=10001, st_gid=10001, st_nlink=1,
                               st_size=len(CONTENT))
    with pytest.raises(evidence.CacheEvidenceError, match=error):
        evidence.inspect_cache(overlay(tmp_path), DIGEST, len(CONTENT), MARKER,
                               guestfs_factory=lambda **kwargs: guest)
    assert guest.calls[-1] == ("close",)


@pytest.mark.parametrize("size, digest, error", [
    (len(CONTENT) + 1, DIGEST, "cache_blob_size_mismatch"),
    (len(CONTENT), "0" * 64, "cache_blob_hash_mismatch"),
])
def test_blob_size_and_hash_are_checked(tmp_path, size, digest, error):
    guest = valid_guest(digest=digest)
    guest.entries[f"/player/cache/{DIGEST}.blob"]["st_size"] = size
    with pytest.raises(evidence.CacheEvidenceError, match=error):
        evidence.inspect_cache(overlay(tmp_path), DIGEST, len(CONTENT), MARKER,
                               guestfs_factory=lambda **kwargs: guest)
    assert guest.calls[-1] == ("close",)


@pytest.mark.parametrize("sha, size, marker, error", [
    ("A" * 64, len(CONTENT), MARKER, "invalid_sha256"),
    (DIGEST, 0, MARKER, "invalid_photo_size"),
    (DIGEST, len(CONTENT), b"", "invalid_state_marker"),
])
def test_arguments_are_bounded_before_guest_creation(tmp_path, sha, size, marker, error):
    created = []

    def factory(**kwargs):
        created.append(True)
        return valid_guest()

    with pytest.raises(evidence.CacheEvidenceError, match=error):
        evidence.inspect_cache(overlay(tmp_path), sha, size, marker, guestfs_factory=factory)
    assert not created


def test_guest_close_is_attempted_when_guest_operation_fails(tmp_path):
    guest = valid_guest(filesystems={"/dev/sda": "ext4"}, close_error="ignored")
    guest.vfs_label = lambda _device: (_ for _ in ()).throw(RuntimeError("probe failure"))
    with pytest.raises(evidence.CacheEvidenceError, match="guest_filesystem_unreadable"):
        evidence.inspect_cache(overlay(tmp_path), DIGEST, len(CONTENT), MARKER,
                               guestfs_factory=lambda **kwargs: guest)
    assert guest.calls[-1] == ("close",)


def test_cli_reports_only_stable_error_code(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["vm-cache-evidence", "--overlay", str(overlay(tmp_path)),
                                       "--sha256", "0" * 64, "--size", "1",
                                       "--state-marker-hex", MARKER.hex()])
    with pytest.raises(SystemExit) as result:
        evidence.main()
    assert result.value.code == 1
    assert json.loads(capsys.readouterr().out) == {"error": "guestfs_unavailable"}
