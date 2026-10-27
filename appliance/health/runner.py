"""Health judge unit (`photo-wall-health.service`, uid pw-health): feeds in, verdict out.

Every 500 ms it drains two node feeds by cursor, one request per connection, repeating while a
page comes back full (at most 32 reads per feed a turn), and hands each fact to the pure judge
with this node's boot clock: the broker's probe feed (`/run/photo-wall-app-feed/feed.sock`) and
the display feed (`/run/photo-wall-display-feed/feed.sock`), whose every page carries Display's
`outputs` snapshot. A gap or a new incarnation of the broker feed makes the judge forget what the
missed facts may have changed; a display feed gap needs nothing (the page's snapshot replaces
the Output set). An unreadable feed (including Display's `response_bound`) leaves the verdict as
it was and is counted.

It serves `/run/photo-wall-health/health.sock` (socket 0666, the operation chosen by the first
packet and admitted per `SO_PEERCRED` uid): `status` (uid 0 only) answers the verdict, the ring
and each feed's read position, then closes; `overlay` (uid 10005, pw-display's overlay client,
only) stays open: the judge pushes each connected Output's `OverlayInstruction` (one packet each)
at once, whenever one changes, and every V/3, and reads back `PresentedReport` packets into the
ring. Each new verdict also goes on its Node's bus, the `health` line's HEALTH_REPORT_KEY
(appliance/health/bus.py), from which Central records it (issue #126): the session runs on its own
thread and the judge only queues. It never connects to the compositor, launches or kills anything,
or talks to Central directly (its unit allows AF_UNIX, and AF_INET to localhost only: the bus).

The judge is built from the published constants: T, k and S from App lifecycle
(`appliance.kernel.probe_timing`), D from Display (`appliance.display_host.overlay.instruction`) and the
catalogue (`contracts.node_faults`). Each card's time is the Pi's own wall clock in its local
zone, read with the boot clock whenever instructions are projected.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import selectors
import socket
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from appliance.display_host.overlay.instruction import (
    INSTRUCTION_STALE_MS,
    PULSE_DEADLINE_MS,
    OverlayInstruction,
    encode_overlay_instruction,
    parse_presented_report,
)
from appliance.feed import READ_LIMIT, FeedCursor, FeedEvent
from appliance.feed_socket import peer_uid
from appliance.health.bus import HEALTH_SLICE, BusHealthSink, health_session
from appliance.health.judge import HealthJudge, Presented, display_outputs
from appliance.health.report import health_report, report_key
from appliance.kernel.clock import boottime_ms
from appliance.kernel.probe_timing import SHIPPED_TIMING
from contracts.node_faults import FAULTS, catalogue_digest
from contracts.strict_json import loads_object

BROKER_FEED_SOCKET = Path("/run/photo-wall-app-feed/feed.sock")
# Display's feed socket for node readers (appliance/display_host/runner.py FEED_SOCKET; the
# judge closure must not import the controller, so the path is named here and pinned by a test).
DISPLAY_FEED_SOCKET = Path("/run/photo-wall-display-feed/feed.sock")
HEALTH_SOCKET = Path("/run/photo-wall-health/health.sock")
READ_PERIOD_SECONDS = 0.5
DRAIN_READS = 32  # pages per feed per turn
FEED_REPLY_WAIT_SECONDS = 1.0  # the broker answers on its main loop: give up this turn, not the feed
MAX_FEED_REPLY = 65536
MAX_REQUEST = 4096
REQUEST_WAIT_SECONDS = 0.05
REQUESTS_PER_TURN = 8
PW_DISPLAY_UID = 10005  # Weston and the overlay client it spawns
# Each operation and the peer uids admitted to it.
OPERATIONS: Mapping[str, frozenset[int]] = {"status": frozenset({0}),
                                            "overlay": frozenset({PW_DISPLAY_UID})}
STREAMS = frozenset({"overlay"})  # operations whose connection stays open
OVERLAY_REFRESH_MS = INSTRUCTION_STALE_MS // 3  # every instruction re-pushed this often (V/3)
MAX_OVERLAY_CLIENTS = 4  # a fifth connection replaces the oldest (a respawned client)
MAX_REPORT = 512
REPORTS_PER_WAKE = 16
SEQPACKET = socket.SOCK_SEQPACKET

log = logging.getLogger(__name__)


def shipped_judge() -> HealthJudge:
    """The judge as released, from the published constants."""
    return HealthJudge(period_ms=SHIPPED_TIMING.period_ms, miss_limit=SHIPPED_TIMING.miss_limit,
                       startup_ms=SHIPPED_TIMING.startup_ms,
                       pulse_deadline_ms=PULSE_DEADLINE_MS, catalogue=FAULTS)


def wall_ms() -> int:
    """The Pi's wall clock in ms: the card's time, never compared with the boot clock."""
    return time.time_ns() // 1_000_000


def local_zone() -> datetime.tzinfo:
    """The Pi's local zone now (read each time, so a daylight-saving change is followed)."""
    return datetime.datetime.now().astimezone().tzinfo


class CardClock:
    """The wall clock and local zone a card's time is read in (`wall_ms`, `local_zone`)."""

    def __init__(self, wall=wall_ms, zone=local_zone):
        self.wall, self.zone = wall, zone

    def instructions(self, judge: HealthJudge, now_ms: int) -> tuple[OverlayInstruction, ...]:
        return judge.instructions(now_ms, wall_ms=self.wall(), zone=self.zone())


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
                page = self._read(self.cursor.request())
                self._check(page)
                events, resnapshot = self.cursor.advance(page)
            except (OSError, ValueError) as error:
                # Unreadable this turn: the verdict stands, the next turn asks again.
                self.failures += 1
                self.last_failure = str(error) if isinstance(error, ValueError) else type(error).__name__
                return
            self.reads += 1
            if resnapshot:
                self.gaps += 1
            self._take(judge, page, events, resnapshot, clock)
            if len(events) < READ_LIMIT:
                return

    def _check(self, page: dict) -> None:
        """Refuse a page before the cursor moves (the broker's pages carry nothing extra)."""

    def _take(self, judge: HealthJudge, page: dict, events: tuple[FeedEvent, ...],
              resnapshot: bool, clock: Callable[[], int]) -> None:
        if resnapshot:
            judge.forget(clock())
        for event in events:
            judge.observe(event, clock())

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


class DisplayFeedReader(FeedReader):
    """Display's feed: events refine, then the page's `outputs` snapshot (current at the reply,
    so newer than its events) replaces the Output set. A gap or a restarted controller needs no
    forgetting: probe-derived state is not Display's, and the snapshot is complete."""

    def _check(self, page: dict) -> None:
        display_outputs(page.get("outputs"))

    def _take(self, judge: HealthJudge, page: dict, events: tuple[FeedEvent, ...],
              resnapshot: bool, clock: Callable[[], int]) -> None:
        for event in events:
            judge.observe_display(event, clock())
        judge.observe_outputs(page["outputs"], clock())


def _instruction_document(instruction: OverlayInstruction) -> dict:
    return {"output": instruction.output, "serial": instruction.serial, "tint": instruction.tint,
            "lines": list(instruction.lines)}


def _ring_document(entry, now_ms: int) -> dict:
    if isinstance(entry, Presented):
        return {"sequence": entry.sequence, "state": "presented", "output": entry.output,
                "serial": entry.serial, "age_ms": now_ms - entry.at_ms}
    return {"sequence": entry.sequence, "code": entry.code, "run": entry.run,
            "state": entry.state, "reason": entry.reason, "age_ms": now_ms - entry.at_ms}


def status_document(judge: HealthJudge, readers: Sequence[FeedReader], now_ms: int, *,
                    overlay_clients: int = 0, card: CardClock | None = None) -> dict:
    """The `status` answer: verdict (with each Output's underlay and card), the overlay instructions,
    the ring (ages on the judge's clock) and feed positions."""
    card = card or CardClock()
    verdict = judge.verdict(now_ms)
    return {
        "verdict": {"sequence": verdict.sequence, "conditions": [
            {"code": condition.code, "run": condition.run, "state": condition.state,
             "age_ms": condition.age_ms} for condition in verdict.conditions],
            "outputs": [{"output": output.output, "underlay": output.underlay,
                         "codes": list(output.codes), "card": output.card,
                         "card_age_ms": output.card_age_ms} for output in verdict.outputs]},
        "overlay": {"instructions": [_instruction_document(instruction)
                                     for instruction in card.instructions(judge, now_ms)],
                    "clients": overlay_clients},
        "ring": [_ring_document(entry, now_ms) for entry in judge.transitions()],
        "ring_dropped": judge.ring_dropped,
        "catalogue": catalogue_digest(),
        "feeds": {reader.name: reader.document() for reader in readers},
    }


class OverlayClients:
    """The open `overlay` connections. Each gets every connected Output's instruction when it
    connects, then the changed ones each turn and all of them every V/3; it sends back
    `PresentedReport`s. A connection that cannot take a packet at once, sends a malformed one or
    closes is dropped (the client reconnects and is re-pushed everything)."""

    def __init__(self, judge: HealthJudge, *, limit: int = MAX_OVERLAY_CLIENTS,
                 refresh_ms: int = OVERLAY_REFRESH_MS, card: CardClock | None = None):
        self.judge, self.limit, self.refresh_ms = judge, limit, refresh_ms
        self.card = card or CardClock()
        self.connections: list[socket.socket] = []
        self.selector: selectors.BaseSelector | None = None
        self._sent: dict[str, OverlayInstruction] = {}
        self._refreshed_ms: int | None = None

    def attach(self, selector: selectors.BaseSelector) -> None:
        self.selector = selector
        for connection in self.connections:
            selector.register(connection, selectors.EVENT_READ, "overlay")

    def adopt(self, connection: socket.socket, now_ms: int) -> None:
        connection.setblocking(False)
        while len(self.connections) >= self.limit:
            self.drop(self.connections[0])
        self.connections.append(connection)
        if self.selector is not None:
            self.selector.register(connection, selectors.EVENT_READ, "overlay")
        for instruction in self.card.instructions(self.judge, now_ms):
            if not self._send(connection, instruction):
                return

    def publish(self, now_ms: int) -> None:
        current = self.card.instructions(self.judge, now_ms)
        refresh = self._refreshed_ms is None or now_ms - self._refreshed_ms >= self.refresh_ms
        due = [instruction for instruction in current
               if refresh or self._sent.get(instruction.output) != instruction]
        self._sent = {instruction.output: instruction for instruction in current}
        if refresh:
            self._refreshed_ms = now_ms
        for connection in list(self.connections):
            for instruction in due:
                if not self._send(connection, instruction):
                    break

    def receive(self, connection: socket.socket, now_ms: int) -> None:
        for _ in range(REPORTS_PER_WAKE):
            try:
                raw = connection.recv(MAX_REPORT + 1)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                self.drop(connection)
                return
            try:
                if not raw or len(raw) > MAX_REPORT:
                    raise ValueError("presented_report")
                report = parse_presented_report(raw)
            except ValueError:
                self.drop(connection)  # closed, or not Display's report language
                return
            self.judge.presented(report, now_ms)

    def drop(self, connection: socket.socket) -> None:
        if connection in self.connections:
            self.connections.remove(connection)
            if self.selector is not None:
                self.selector.unregister(connection)
        connection.close()

    def close(self) -> None:
        for connection in list(self.connections):
            self.drop(connection)

    def _send(self, connection: socket.socket, instruction: OverlayInstruction) -> bool:
        try:
            connection.send(encode_overlay_instruction(instruction))
            return True
        except OSError:  # full (EAGAIN) or gone: the client reconnects
            self.drop(connection)
            return False


class HealthSocket:
    """`health.sock`: one request per accept, the operation admitted per peer uid.

    A peer admitted to no operation is closed unread with no reply; an admitted peer naming an
    operation it is not admitted to is closed with no reply; a malformed request from an
    admitted peer gets `health_request`. An admitted `overlay` request gets no reply: its
    connection is handed to `stream` and stays open."""

    def __init__(self, path: Path, answer: Callable[[str], dict], *,
                 stream: Callable[[str, socket.socket], None] | None = None,
                 operations: Mapping[str, frozenset[int]] = OPERATIONS, peer=peer_uid,
                 kind: int = SEQPACKET, owner_uid: int | None = None):
        self.path, self.answer, self.operations, self.peer = path, answer, operations, peer
        self.stream = stream  # takes an admitted STREAMS connection (it stays open)
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
            if not self._serve_one(connection):
                connection.close()

    def _serve_one(self, connection: socket.socket) -> bool:
        """Answer one accepted connection; True iff it was handed on (and stays open)."""
        try:
            uid = self.peer(connection)
            if uid not in self.peers:
                return False  # admitted to nothing: closed unread
            connection.settimeout(REQUEST_WAIT_SECONDS)
            raw = connection.recv(MAX_REQUEST + 1)
        except OSError:
            return False
        request = loads_object(raw, max_bytes=MAX_REQUEST)
        operation = request.get("op") if request is not None and set(request) == {"op"} else None
        if operation not in self.operations or (operation in STREAMS and self.stream is None):
            reply = {"accepted": False, "reason": "health_request"}
        elif uid not in self.operations[operation]:
            return False  # not admitted to this operation: no reply
        elif operation in STREAMS:
            self.stream(operation, connection)
            return True
        else:
            reply = {"accepted": True, **self.answer(operation)}
        try:
            connection.sendall(json.dumps(reply, separators=(",", ":")).encode())
        except OSError:
            pass
        return False

    def close(self) -> None:
        self.listener.close()
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if (info.st_dev, info.st_ino) == self.identity:
            self.path.unlink()


class HealthRunner:
    """One judge, its feed readers, its socket and the overlay clients on one thread: reads
    and publishes every READ_PERIOD, answers requests and reads reports in between. With a
    `sink`, each turn whose report differs from the last one put (appliance/health/report.py
    `report_key`) hands the verdict to it as a HealthReport (appliance/health/report.py); the sink only queues, so the judge never
    waits on the bus, and a verdict the contract refuses is logged and skipped."""

    def __init__(self, judge: HealthJudge, readers: Sequence[FeedReader], *,
                 clock: Callable[[], int] = boottime_ms, wall: Callable[[], int] = wall_ms,
                 sink: BusHealthSink | None = None, card: CardClock | None = None):
        self.judge, self.readers, self.clock, self.wall, self.sink = judge, tuple(readers), clock, wall, sink
        self.card = card or CardClock()
        self.overlay = OverlayClients(judge, card=self.card)
        self._put_key: tuple | None = None  # what the report last handed to the sink said

    def turn(self) -> None:
        for reader in self.readers:
            reader.drain(self.judge, self.clock)
        self._report(self.clock())
        self.overlay.publish(self.clock())

    def _report(self, now_ms: int) -> None:
        if self.sink is None:
            return
        verdict = self.judge.verdict(now_ms)
        key = report_key(verdict, now_ms)
        if key == self._put_key:
            return
        self._put_key = key
        try:
            self.sink.put(health_report(verdict, now_ms=now_ms, wall_ms=self.wall(),
                                        catalogue=catalogue_digest()))
        except ValueError as error:   # outside the contract: the overlay still shows it
            log.warning("health verdict %s not put on the bus: %s", verdict.sequence, error)

    def answer(self, operation: str) -> dict:
        if operation != "status":
            raise ValueError("health_request")
        return status_document(self.judge, self.readers, self.clock(),
                               overlay_clients=len(self.overlay.connections), card=self.card)

    def stream(self, operation: str, connection: socket.socket) -> None:
        if operation != "overlay":
            raise ValueError("health_request")
        self.overlay.adopt(connection, self.clock())

    def run(self, server: HealthSocket, *, period: float = READ_PERIOD_SECONDS) -> None:
        with selectors.DefaultSelector() as selector:
            selector.register(server.listener, selectors.EVENT_READ, "listener")
            self.overlay.attach(selector)
            due = time.monotonic()
            while True:
                if time.monotonic() >= due:
                    self.turn()
                    due = time.monotonic() + period
                for key, _mask in selector.select(timeout=max(0.0, due - time.monotonic())):
                    if key.data == "listener":
                        server.serve()
                    else:
                        self.overlay.receive(key.fileobj, self.clock())


def main() -> None:
    # The health line's session: it connects on its own thread forever, so the judge never waits on
    # the bus, and it is kept for the process's life.
    session = health_session(HEALTH_SLICE.digest)
    session.start()
    runner = HealthRunner(shipped_judge(), (FeedReader("broker", BROKER_FEED_SOCKET),
                                            DisplayFeedReader("display", DISPLAY_FEED_SOCKET)),
                          sink=BusHealthSink(session))
    server = HealthSocket(HEALTH_SOCKET, runner.answer, stream=runner.stream)
    try:
        runner.run(server)
    finally:
        runner.overlay.close()
        server.close()
        session.stop()


if __name__ == "__main__":
    main()
