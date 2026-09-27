"""The one atomic write a root stage leaves for a later stage: explicit modes, whatever the
umask (stage-2 units run UMask=0077, and the Player runs as `wall`)."""

import os
import stat
from contextlib import contextmanager

import pytest

from uplink.files import write_atomically


@contextmanager
def umask(mask):
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_under_umask_077_created_parents_are_0755_and_the_file_has_its_mode(tmp_path):
    path = tmp_path / "etc" / "photo-wall" / "public.json"
    with umask(0o077):
        write_atomically(path, b"{}", mode=0o644)
    assert path.read_bytes() == b"{}"
    assert (mode(tmp_path / "etc"), mode(path.parent), mode(path)) == (0o755, 0o755, 0o644)


def test_an_existing_parent_keeps_its_own_mode(tmp_path):
    existing = tmp_path / "run"
    existing.mkdir(mode=0o700)
    os.chmod(existing, 0o700)
    with umask(0o077):
        write_atomically(existing / "record.json", b"1", mode=0o644)
        write_atomically(existing / "record.json", b"2", mode=0o644)
    assert mode(existing) == 0o700
    assert (existing / "record.json").read_bytes() == b"2"
    assert [entry.name for entry in existing.iterdir()] == ["record.json"]


def test_a_failed_write_leaves_the_old_file_and_no_temporary(tmp_path):
    path = tmp_path / "public.json"
    path.write_bytes(b"old")
    with pytest.raises(TypeError):
        write_atomically(path, "not bytes", mode=0o644)
    assert path.read_bytes() == b"old"
    assert [entry.name for entry in tmp_path.iterdir()] == ["public.json"]
