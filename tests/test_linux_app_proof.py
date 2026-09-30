"""The Linux adapter fails closed before any proof is promoted to OS evidence."""

import os
import socket
import struct
import sys
from pathlib import Path

import pytest

from appliance import linux_app_proof
from appliance.linux_app_proof import LinuxAppProofSamplers
from contracts.app_process_proof import ProcessIdentity

UNIT = "photo-wall-player.service"
CGROUP = "/system.slice/" + UNIT
INVOCATION = "a" * 32


def show(*, pid=123, invocation=INVOCATION, state="active", cgroup=CGROUP):
    return (f"MainPID={pid}\nInvocationID={invocation}\nActiveState={state}\n"
            f"ControlGroup={cgroup}\n")


def proc(tmp_path: Path, *, pid=123, ticks=456, cgroup=CGROUP):
    directory = tmp_path / str(pid)
    directory.mkdir(exist_ok=True)
    (directory / "stat").write_text(
        f"{pid} (odd ) name) S " + " ".join(["0"] * 18 + [str(ticks)]))
    (directory / "cgroup").write_text(f"0::{cgroup}\n")


@pytest.mark.skipif(sys.platform != "linux", reason="Linux AF_UNIX SOCK_SEQPACKET required")
def test_kernel_peer_credentials_come_from_the_supplied_connected_socket():
    first, second = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    seen = []
    try:
        def credentials(connection):
            seen.append(connection)
            return struct.pack("=iII", 123, 10001, 10001)

        adapter = LinuxAppProofSamplers(peercred_reader=credentials)
        assert adapter.peer_credentials(first).pid == 123
        assert adapter.peer_credentials(first).uid == 10001
        assert seen == [first, first]
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            with pytest.raises(OSError):
                adapter.peer_credentials(listener)
        finally:
            listener.close()
        with pytest.raises(TypeError):
            adapter.peer_credentials(object())
    finally:
        first.close()
        second.close()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux SO_PEERCRED required")
def test_real_linux_peercred_is_kernel_connection_snapshot():
    first, second = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        observed = LinuxAppProofSamplers().peer_credentials(first)
        assert (observed.pid, observed.uid, observed.gid) == (
            os.getpid(), os.getuid(), os.getgid())
    finally:
        first.close()
        second.close()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux AF_UNIX SOCK_SEQPACKET required")
def test_peer_credentials_reject_wrong_protocol_and_malformed_kernel_record():
    first, second = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        adapter = LinuxAppProofSamplers(peercred_reader=lambda _: b"short")
        with pytest.raises(TypeError):
            adapter.peer_credentials(first)
    finally:
        first.close()
        second.close()
    first, second = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        with pytest.raises(ValueError, match="peercred_invalid"):
            LinuxAppProofSamplers(peercred_reader=lambda _: b"short").peer_credentials(first)
        with pytest.raises(ValueError, match="peercred_invalid"):
            LinuxAppProofSamplers(peercred_reader=lambda _:
                                  struct.pack("=iII", 0, 10001, 10001)).peer_credentials(first)
    finally:
        first.close()
        second.close()


def test_stable_pid1_and_proc_identity_for_main_and_peer(tmp_path):
    proc(tmp_path)
    adapter = LinuxAppProofSamplers(proc_root=tmp_path, show_reader=show)
    expected = ProcessIdentity(pid=123, start_ticks=456, invocation_id=INVOCATION,
                               cgroup_unit=UNIT)
    assert adapter.main_process() == expected
    assert adapter.peer_process(123) == expected
    assert adapter.peer_process(124) is None


@pytest.mark.parametrize("bad_show", [
    show(state="inactive"), show(pid=0), show(invocation="bad"),
    show(cgroup="/system.slice/other.service"),
    show() + "MainPID=123\n", show().replace("ControlGroup=", "Bogus="),
])
def test_pid1_invalid_or_ambiguous_fails_closed(tmp_path, bad_show):
    proc(tmp_path)
    assert LinuxAppProofSamplers(proc_root=tmp_path,
                                 show_reader=lambda: bad_show).main_process() is None


@pytest.mark.parametrize("second", [
    show(pid=124), show(invocation="b" * 32), show(state="inactive"),
    show(cgroup="/other.slice/" + UNIT),
])
def test_pid1_race_fails_closed(tmp_path, second):
    proc(tmp_path)
    samples = iter([show(), second])
    assert LinuxAppProofSamplers(proc_root=tmp_path,
                                 show_reader=lambda: next(samples)).main_process() is None


def test_proc_cgroup_and_start_tick_races_fail_closed(tmp_path, monkeypatch):
    proc(tmp_path)
    adapter = LinuxAppProofSamplers(proc_root=tmp_path, show_reader=show)
    (tmp_path / "123" / "cgroup").write_text("0::/system.slice/other.service\n")
    assert adapter.main_process() is None
    (tmp_path / "123" / "cgroup").write_text("0::" + CGROUP + "\n0::" + CGROUP)
    assert adapter.main_process() is None
    proc(tmp_path)
    ticks = iter([456, 457])
    monkeypatch.setattr(linux_app_proof, "read_proc_start_ticks", lambda *_: next(ticks))
    assert adapter.main_process() is None


def test_bounded_proc_stat_reader_rejects_missing_and_oversized_records(tmp_path):
    proc(tmp_path)
    (tmp_path / "123" / "stat").write_bytes(b"X" * 4097)
    assert LinuxAppProofSamplers(proc_root=tmp_path, show_reader=show).main_process() is None
    (tmp_path / "123" / "stat").unlink()
    assert LinuxAppProofSamplers(proc_root=tmp_path, show_reader=show).main_process() is None
