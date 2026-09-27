"""uplink.watchdog against a real AF_UNIX SOCK_DGRAM socket, per the sd_notify protocol."""

import os
import socket
import sys
import tempfile

import pytest

from uplink import watchdog


@pytest.fixture
def notify_socket(monkeypatch):
    directory = tempfile.mkdtemp(dir="/tmp")
    path = os.path.join(directory, "s")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(path)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    yield sock
    sock.close()


def _recv(sock: socket.socket) -> str:
    sock.settimeout(1)
    return sock.recv(4096).decode()


def test_notify_joins_assignments_with_newline(notify_socket):
    assert watchdog.notify("A=1", "B=2") is True
    assert _recv(notify_socket) == "A=1\nB=2"


def test_ready(notify_socket):
    assert watchdog.ready() is True
    assert _recv(notify_socket) == "READY=1"


def test_pet(notify_socket):
    assert watchdog.pet() is True
    assert _recv(notify_socket) == "WATCHDOG=1"


def test_extend_start(notify_socket):
    assert watchdog.extend_start(1.5) is True
    assert _recv(notify_socket) == "EXTEND_TIMEOUT_USEC=1500000"


def test_status_flattens_newlines(notify_socket):
    assert watchdog.status("line one\nline two") is True
    assert _recv(notify_socket) == "STATUS=line one line two"


def test_no_notify_socket_returns_false(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert watchdog.notify("READY=1") is False


def test_dead_socket_returns_false_without_raising(monkeypatch):
    directory = tempfile.mkdtemp(dir="/tmp")
    path = os.path.join(directory, "gone")
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    assert watchdog.notify("READY=1") is False


@pytest.mark.skipif(sys.platform == "darwin", reason="abstract sockets are Linux-only")
def test_abstract_socket_prefix(monkeypatch):
    name = "\0photo-wall-test-watchdog"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(name)
    monkeypatch.setenv("NOTIFY_SOCKET", "@" + name[1:])
    try:
        assert watchdog.notify("READY=1") is True
        assert _recv(sock) == "READY=1"
    finally:
        sock.close()
