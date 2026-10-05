"""Health judge unit (`photo-wall-health.service`, uid pw-health): feeds in, verdict out.

Every 500 ms it drains the broker's node feed (`/run/photo-wall-app-feed/feed.sock`) by cursor,
one request per connection, repeating while a page comes back full (at most 32 reads a turn),
and hands each fact to the pure judge with this node's boot clock. A gap or a new publisher
incarnation makes the judge forget what the missed facts may have changed; an unreadable feed
leaves the verdict as it was. It serves `/run/photo-wall-health/health.sock` (socket 0666, the
operation chosen per request and admitted per `SO_PEERCRED` uid): `status` (uid 0 only) answers
the verdict, the transition ring and each feed's read position. It never connects to the
compositor, launches or kills anything, or talks to Central (its unit allows AF_UNIX only).

The judge is built from the published constants: T, k, S and K from App lifecycle
(`appliance.node.probe`), D from Display (`appliance.display_host.overlay.instruction`) and the
catalogue (`contracts.node_faults`); a K that could kill before the card shows refuses to start.
"""

from __future__ import annotations

import json
import os
import selectors
import socket
import stat
import struct
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from appliance.clock import boottime_ms
from appliance.display_host.overlay.instruction import PULSE_DEADLINE_MS
from appliance.feed import READ_LIMIT, FeedCursor
from appliance.health.judge import HealthJudge
from appliance.node.probe import SHIPPED_TIMING
from contracts.node_faults import FAULTS, catalogue_digest
from contracts.strict_json import loads_object

BROKER_FEED_SOCKET = Path("/run/photo-wall-app-feed/feed.sock")
HEALTH_SOCKET = Path("/run/photo-wall-health/health.sock")
READ_PERIOD_SECONDS = 0.5
DRAIN_READS = 32  # pages per feed per turn
FEED_REPLY_WAIT_SECONDS = 1.0  # the broker answers on its main loop: give up this turn, not the feed
MAX_FEED_REPLY = 65536
MAX_REQUEST = 4096
REQUEST_WAIT_SECONDS = 0.05
REQUESTS_PER_TURN = 8
# Each operation and the peer uids admitted to it (B10b adds `overlay` for pw-display).
OPERATIONS: Mapping[str, frozenset[int]] = {"status": frozenset({0})}
SEQPACKET = getattr(socket, "SOCK_SEQPACKET", socket.SOCK_STREAM)


def shipped_judge() -> HealthJudge:
    """The judge as released: refuses to construct if the shipped K breaks the K rule."""
    return HealthJudge(period_ms=SHIPPED_TIMING.period_ms, miss_limit=SHIPPED_TIMING.miss_limit,
                       startup_ms=SHIPPED_TIMING.startup_ms,
                       kill_after_ms=SHIPPED_TIMING.kill_after_ms,
                       pulse_deadline_ms=PULSE_DEADLINE_MS, catalogue=FAULTS)


def peer_uid(connection: socket.socket) -> int:
    # Same reading as the broker's feed listener; B10a lifts both into the kernel's feed socket.
    size = struct.calcsize("=iII")
    _pid, uid, _gid = struct.unpack("=iII", connection.getsockopt(
        socket.SOL_SOCKET, getattr(socket, "SO_PEERCRED", 17), size))
    return uid


class FeedReader:
    """One publisher's feed, read by cursor (the publisher answers one request per accept)."""

    def __init__(self, name: str, path: Path, *, kind: int = SEQPACKET,
                 wait: float = FEED_REPLY_WAIT_SECONDS):
        self.name, self.path, self.kind, self.wait = name, path, kind, wait
        self.cursor = FeedCursor()
        self.reads = self.gaps = self.failures = 0
        self.last_failure: str | None = None

    def drain(self, judge: HealthJudge, clock: Callable[[], int]) -> None:
        for _ in range(DRAIN_READS):
            try:
                events, resnapshot = self.cursor.advance(self._read(self.cursor.request()))
            except (OSError, ValueError) as error:
                # Unreadable this turn: the verdict stands, the next turn asks again.
                self.failures += 1
                self.last_failure = str(error) if isinstance(error, ValueError) else type(error).__name__
                return
            self.reads += 1
            if resnapshot:
                self.gaps += 1
                judge.forget(clock())
            for event in events:
                judge.observe(event, clock())
            if len(events) < READ_LIMIT:
                return

    def _read(self, request: dict) -> dict:
        with socket.socket(socket.AF_UNIX, self.kind) as connection:
            connection.settimeout(self.wait)
            connection.connect(str(self.path))
            connection.sendall(json.dumps(request, separators=(",", ":")).encode())
            raw = connection.recv(MAX_FEED_REPLY + 1)
        reply = loads_object(raw, max_bytes=MAX_FEED_REPLY)
        if reply is None or reply.get("accepted") is not True:
            raise ValueError("feed_read_refused")
        return reply

    def document(self) -> dict:
        incarnation = self.cursor.incarnation
        return {"socket": str(self.path), "after": self.cursor.after,
                "publisher_incarnation": None if incarnation is None else str(incarnation),
                "reads": self.reads, "gaps": self.gaps, "failures": self.failures,
                "last_failure": self.last_failure}


def status_document(judge: HealthJudge, readers: Sequence[FeedReader], now_ms: int) -> dict:
    """The `status` answer: verdict, transition ring (ages on the judge's clock), feed positions."""
    verdict = judge.verdict(now_ms)
    return {
        "verdict": {"sequence": verdict.sequence, "conditions": [
            {"code": condition.code, "run": condition.run, "state": condition.state,
             "age_ms": condition.age_ms} for condition in verdict.conditions]},
        "ring": [{"sequence": entry.sequence, "code": entry.code, "run": entry.run,
                  "state": entry.state, "reason": entry.reason, "age_ms": now_ms - entry.at_ms}
                 for entry in judge.transitions()],
        "ring_dropped": judge.ring_dropped,
        "catalogue": catalogue_digest(),
        "feeds": {reader.name: reader.document() for reader in readers},
    }


class HealthSocket:
    """`health.sock`: one request per accept, the operation admitted per peer uid.

    A peer admitted to no operation is closed unread with no reply; an admitted peer naming an
    operation it is not admitted to is closed with no reply; a malformed request from an
    admitted peer gets `health_request`."""

    def __init__(self, path: Path, answer: Callable[[str], dict], *,
                 operations: Mapping[str, frozenset[int]] = OPERATIONS, peer=peer_uid,
                 kind: int = SEQPACKET, owner_uid: int | None = None):
        self.path, self.answer, self.operations, self.peer = path, answer, operations, peer
        self.owner_uid = os.geteuid() if owner_uid is None else owner_uid
        self.peers = frozenset().union(*operations.values())
        directory = path.parent.lstat()
        if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != self.owner_uid
                or directory.st_mode & 0o022):
            raise ValueError("health_directory")
        self._remove_stale()
        self.listener = socket.socket(socket.AF_UNIX, kind)
        try:
            self.listener.bind(str(path))
            os.chmod(path, 0o666)  # any local peer may connect; each operation checks the uid
            info = path.lstat()
            self.identity = info.st_dev, info.st_ino
            self.listener.listen(8)
            self.listener.setblocking(False)
        except BaseException:
            self.listener.close()
            raise

    def _remove_stale(self) -> None:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.owner_uid:
            raise ValueError("health_socket_ownership")
        self.path.unlink()

    def serve(self, budget: int = REQUESTS_PER_TURN) -> None:
        for _ in range(budget):
            try:
                connection, _ = self.listener.accept()
            except (BlockingIOError, InterruptedError):
                return
            with connection:
                try:
                    uid = self.peer(connection)
                    if uid not in self.peers:
                        continue  # admitted to nothing: closed unread
                    connection.settimeout(REQUEST_WAIT_SECONDS)
                    raw = connection.recv(MAX_REQUEST + 1)
                except OSError:
                    continue
                request = loads_object(raw, max_bytes=MAX_REQUEST)
                operation = request.get("op") if request is not None and set(request) == {"op"} else None
                if operation not in self.operations:
                    reply = {"accepted": False, "reason": "health_request"}
                elif uid not in self.operations[operation]:
                    continue  # not admitted to this operation: no reply
                else:
                    reply = {"accepted": True, **self.answer(operation)}
                try:
                    connection.sendall(json.dumps(reply, separators=(",", ":")).encode())
                except OSError:
                    pass

    def close(self) -> None:
        self.listener.close()
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if (info.st_dev, info.st_ino) == self.identity:
            self.path.unlink()


class HealthRunner:
    """One judge, its feed readers and its socket on one thread: reads every READ_PERIOD,
    answers requests in between."""

    def __init__(self, judge: HealthJudge, readers: Sequence[FeedReader], *,
                 clock: Callable[[], int] = boottime_ms):
        self.judge, self.readers, self.clock = judge, tuple(readers), clock

    def turn(self) -> None:
        for reader in self.readers:
            reader.drain(self.judge, self.clock)

    def answer(self, operation: str) -> dict:
        if operation != "status":
            raise ValueError("health_request")
        return status_document(self.judge, self.readers, self.clock())

    def run(self, server: HealthSocket, *, period: float = READ_PERIOD_SECONDS) -> None:
        with selectors.DefaultSelector() as selector:
            selector.register(server.listener, selectors.EVENT_READ)
            due = time.monotonic()
            while True:
                if time.monotonic() >= due:
                    self.turn()
                    due = time.monotonic() + period
                if selector.select(timeout=max(0.0, due - time.monotonic())):
                    server.serve()


def main() -> None:
    runner = HealthRunner(shipped_judge(), (FeedReader("broker", BROKER_FEED_SOCKET),))
    server = HealthSocket(HEALTH_SOCKET, runner.answer)
    try:
        runner.run(server)
    finally:
        server.close()


if __name__ == "__main__":
    main()
