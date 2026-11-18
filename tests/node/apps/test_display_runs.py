"""The broker tells Display which Player run is current (decision 0034, S4b part 4, E3), on real
sockets: `DisplayRuns` against a SOCK_SEQPACKET listener with SO_PASSCRED in a temp directory,
answered by the display controller's own ingress reader (`runner.answer_ingress`) into a real
`Controller`, so the run lands in its `Handoffs`.

It is told once per change, again after a refusal or a missing socket, and again when the socket
is bound anew (a new controller, a new inode); otherwise a turn costs only a `stat`. Mutation
probe 4.3: key the delivery on `run` alone -> the rebound-socket test fails."""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from appliance.apps.display_runs import DisplayRuns
from appliance.apps.probe import AppRunKey
from appliance.display_host.handoff import AppRun
from appliance.display_host.runner import Controller, answer_ingress

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="the ingress is SOCK_SEQPACKET with SO_PASSCRED (Linux)")

FIRST = AppRunKey(uuid4(), 396, 31751781, 1)
SECOND = AppRunKey(uuid4(), 397, 31751790, 2)


class Backend:
    def initialize(self):
        return SimpleNamespace(boot_id=uuid4(), incarnation_id=uuid4(), states=lambda: ())


class Ingress:
    """A display controller's ingress on `path`: each connection answered by `answer_ingress`
    (this test's uid admitted), or refused outright while `refusing`; `packets` counts them."""

    def __init__(self, path: Path) -> None:
        self.path, self.controller = path, Controller(Backend())
        self.packets, self.refusing = 0, False
        self.stopping = threading.Event()
        self.listener = self._bind()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _bind(self) -> socket.socket:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        listener.bind(str(self.path))
        listener.listen(4)
        listener.settimeout(0.05)
        return listener

    def _serve(self) -> None:
        while not self.stopping.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            with connection:
                connection.settimeout(1)
                self.packets += 1
                if self.refusing:
                    connection.recv(4096)
                    connection.sendall(b'{"accepted":false,"reason":"ValueError"}')
                else:
                    answer_ingress(connection, self.controller, root=os.getuid())

    def close(self) -> None:
        self.stopping.set()
        self.thread.join(5)
        self.listener.close()
        self.path.unlink(missing_ok=True)
        # A socket's ctime has the kernel's coarse clock (a few ms): a controller bound again
        # within it, on a reused inode, would look the same. A real one takes seconds.
        time.sleep(0.05)

    @property
    def run(self) -> AppRun | None:
        return self.controller.handoffs.current_run


def held(key: AppRunKey) -> AppRun:
    return AppRun.of(key.document())


@pytest.fixture
def ingress_path():
    # A short directory: an AF_UNIX path has 108 bytes.
    directory = Path(f"/tmp/pw-runs-{uuid4().hex[:8]}")
    directory.mkdir()
    try:
        yield directory / "ingress.sock"
    finally:
        for child in directory.iterdir():
            child.unlink()
        directory.rmdir()


def test_display_is_told_once_per_change_and_otherwise_only_stat(ingress_path):
    ingress = Ingress(ingress_path)
    try:
        runs = DisplayRuns(ingress_path)
        assert runs.tell(FIRST) is True
        assert (ingress.packets, ingress.run) == (1, held(FIRST))
        for _ in range(5):                                 # unchanged: a stat each, no packet
            assert runs.tell(FIRST) is True
        assert ingress.packets == 1
        assert runs.tell(SECOND) is True
        assert (ingress.packets, ingress.run) == (2, held(SECOND))
        assert runs.tell(None) is True                     # no run is current
        assert (ingress.packets, ingress.run) == (3, None)
        assert runs.tell(None) is True
        assert ingress.packets == 3
    finally:
        ingress.close()


def test_a_refusal_is_told_again_at_the_next_call(ingress_path, caplog):
    ingress = Ingress(ingress_path)
    try:
        ingress.refusing = True
        runs = DisplayRuns(ingress_path)
        assert runs.tell(FIRST) is False
        assert runs.tell(FIRST) is False
        assert ingress.packets == 2                        # sent again, not remembered as held
        assert [record.getMessage() for record in caplog.records].count(
            "broker: Display was not told the current run: refused (ValueError); telling it again") == 1
        ingress.refusing = False
        assert runs.tell(FIRST) is True
        assert (ingress.packets, ingress.run) == (3, held(FIRST))
        assert runs.tell(FIRST) is True
        assert ingress.packets == 3
    finally:
        ingress.close()


def test_a_missing_socket_is_told_once_it_is_there(ingress_path):
    runs = DisplayRuns(ingress_path, timeout=0.5)
    assert runs.tell(FIRST) is False                       # no controller yet: no OSError raised
    ingress = Ingress(ingress_path)
    try:
        assert runs.tell(FIRST) is True
        assert (ingress.packets, ingress.run) == (1, held(FIRST))
    finally:
        ingress.close()


def test_a_new_controller_is_told_the_same_run_again(ingress_path):
    ingress = Ingress(ingress_path)
    runs = DisplayRuns(ingress_path)
    try:
        assert runs.tell(FIRST) is True
    finally:
        ingress.close()
    assert runs.tell(FIRST) is False                       # the controller is gone
    rebound = Ingress(ingress_path)                        # a new controller: a new inode, no run held
    try:
        assert rebound.run is None
        assert runs.tell(FIRST) is True
        assert (rebound.packets, rebound.run) == (1, held(FIRST))
    finally:
        rebound.close()


def test_a_new_controller_bound_between_two_calls_is_told_again(ingress_path):
    """The socket replaced with no call in between (Display restarted within one broker turn)."""
    ingress = Ingress(ingress_path)
    runs = DisplayRuns(ingress_path)
    try:
        assert runs.tell(FIRST) is True
    finally:
        ingress.close()
    rebound = Ingress(ingress_path)
    try:
        assert runs.tell(FIRST) is True
        assert (rebound.packets, rebound.run) == (1, held(FIRST))
    finally:
        rebound.close()
