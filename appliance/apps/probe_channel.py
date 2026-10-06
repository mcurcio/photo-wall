"""The broker's probe thread: its own selector and deadline timer, so probe timing never
waits on the main loop (which blocks on systemctl and HTTP).

The main loop hands over admitted channels (`adopt`), publishes the current app run each
turn (`publish_run`), restates the relink it owes (`owe_relink`) and collects kill decisions
(`take_kill_due`); it alone actuates. This thread only measures: it sends a nonce every T on
the current run's channel, matches answers, and appends probe facts (audience node) to the
broker feed. It never calls systemctl, HTTP, the store or the driver. A channel is judged
against every publication after its adoption: one whose run is not the published run is
closed. An owed relink is a level, not a packet: each channel instance for the owed run gets
it once per owed episode (each Central refusal is a new episode), so a relink owed while no
channel is open reaches the next one and a later refusal reaches a long-lived channel again. Kill-due is a level
too: the latch is re-asserted on every turn while the run stays unanswered for at least K
(the `probe_kill_due` fact stays once per episode) and dropped by an answer, so a kill the main
loop withheld (an armed recovery) is offered again until it is taken and carried out or the
run answers.
"""
from __future__ import annotations

import logging
import secrets
import selectors
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass

from appliance.apps.probe import (
    SHIPPED_TIMING,
    AppRunKey,
    KillDue,
    OwedRelink,
    ProbeClock,
    ProbeTiming,
)
from appliance.feed import Feed
from appliance.kernel.clock import boottime_ms
from contracts.node_app_link import (
    MAX_NODE_LINK_BYTES,
    encode_node_probe,
    encode_node_relink,
    parse_node_probe_answer,
)

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class _Channel:
    connection: socket.socket
    run: AppRunKey
    adopted_at: int  # publication sequence when the main loop handed it over
    relinked: str | None = None  # the owed episode this channel instance already carried


class ProbeThread:
    def __init__(self, feed: Feed, *, clock: Callable[[], int] = boottime_ms,
                 timing: ProbeTiming = SHIPPED_TIMING):
        self.feed, self._now, self.timing = feed, clock, timing
        self._lock = threading.Lock()
        # Main loop -> thread (under the lock).
        self._publication: tuple[int, AppRunKey | None, bool] = (0, None, False)
        self._adoptions: list[_Channel] = []
        self._owed: OwedRelink | None = None
        # Thread -> main loop (under the lock).
        self._kill_due: KillDue | None = None
        # Thread-owned.
        self._applied = 0
        self._probes: ProbeClock | None = None
        self._channel: _Channel | None = None
        self._stopping = False
        self._selector = selectors.DefaultSelector()
        self._wake_reader, self._wake_writer = socket.socketpair()
        self._wake_reader.setblocking(False)
        self._wake_writer.setblocking(False)
        self._selector.register(self._wake_reader, selectors.EVENT_READ, None)
        self._thread = threading.Thread(target=self._run, name="broker-probe", daemon=True)

    # -- main loop side -------------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def check(self) -> None:
        """Raise if the thread stopped: probes must never silently cease."""
        if not self._thread.is_alive():
            raise RuntimeError("probe_thread_stopped")

    def close(self) -> None:
        with self._lock:
            self._stopping = True
        self._wake()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        with self._lock:
            pending, self._adoptions = self._adoptions, []
        for channel in pending:
            channel.connection.close()
        if self._channel is not None:
            self._channel.connection.close()
            self._channel = None
        self._selector.close()
        self._wake_reader.close()
        self._wake_writer.close()

    def adopt(self, connection: socket.socket, run: AppRunKey) -> None:
        """Take an admitted `probe_open` connection for `run`; the caller must not close it."""
        with self._lock:
            if self._stopping:
                connection.close()
                return
            self._adoptions.append(_Channel(connection, run, self._publication[0]))
        self._wake()

    def publish_run(self, run: AppRunKey | None, recovery_may_be_armed: bool) -> None:
        """The main loop's current app run and its kill guard, every turn."""
        with self._lock:
            sequence, previous, _ = self._publication
            self._publication = (sequence + 1, run, bool(recovery_may_be_armed))
        if run != previous:
            self._wake()

    @property
    def recovery_may_be_armed(self) -> bool:
        with self._lock:
            return self._publication[2]

    def take_kill_due(self) -> KillDue | None:
        """Take the kill-due latch; the thread re-asserts it next turn while still overdue."""
        with self._lock:
            due, self._kill_due = self._kill_due, None
            return due

    def owe_relink(self, owed: OwedRelink | None) -> None:
        """The relink the outbox owes a run's Player (Central refused its link), every turn.

        Idempotent and level-triggered; `None` = nothing owed. Each channel instance for the
        owed run carries each owed episode once, on adoption or at once if already open.
        """
        with self._lock:
            changed, self._owed = self._owed != owed, owed
        if changed:
            self._wake()

    def _wake(self) -> None:
        try:
            self._wake_writer.send(b"\0")
        except OSError:
            pass  # a full wake pipe already wakes the thread; a closed one is shutting down

    # -- probe thread ---------------------------------------------------------------------

    def _run(self) -> None:
        period = self.timing.period_ms
        deadline = self._now() + period
        while True:
            with self._lock:
                if self._stopping:
                    return
            now = self._now()
            for key, _ in self._selector.select(max(0, deadline - now) / 1000):
                if key.data is None:
                    try:
                        self._wake_reader.recv(4096)
                    except BlockingIOError:
                        pass
                elif self._channel is not None and key.fileobj is self._channel.connection:
                    self._receive(self._now())
            now = self._now()
            self._apply(now)
            if now >= deadline:
                # A turn more than T/2 behind its deadline: this thread stalled, not the app.
                self._turn(now, late=now - deadline > period // 2)
                deadline += period
                if deadline <= now:
                    deadline = now + period

    def _apply(self, now: int) -> None:
        with self._lock:
            sequence, run, _ = self._publication
            adoptions, self._adoptions = self._adoptions, []
        if sequence != self._applied:
            self._applied = sequence
            current = self._probes.run if self._probes is not None else None
            if run != current:
                self._probes = ProbeClock(run, now, timing=self.timing) if run is not None else None
                with self._lock:
                    if self._kill_due is not None and self._kill_due.run != run:
                        self._kill_due = None
                    if self._owed is not None and self._owed.run != run:
                        self._owed = None
        for channel in adoptions:
            self._close("superseded")  # one channel: the newest open wins
            try:
                channel.connection.setblocking(False)
                self._selector.register(channel.connection, selectors.EVENT_READ, "channel")
            except (OSError, ValueError):
                channel.connection.close()
                continue
            self._channel = channel
            self.feed.append("probe_channel", {"run": channel.run.document(), "state": "open"})
        if (self._channel is not None and self._applied > self._channel.adopted_at
                and self._channel.run != run):
            self._close("run_changed")
        self._relink()

    def _relink(self) -> None:
        """Send each owed episode once on this channel; unsent (EAGAIN) is retried next pass."""
        channel = self._channel
        with self._lock:
            owed = self._owed
        if (channel is None or owed is None or channel.run != owed.run
                or channel.relinked == owed.episode):
            return
        if self._send(encode_node_relink()):
            channel.relinked = owed.episode
            self.feed.append("relink_sent", {"run": owed.run.document()})

    def _turn(self, now: int, *, late: bool) -> None:
        probes = self._probes
        if probes is None:
            return
        for fact in probes.turn(now, late=late):
            self.feed.append(fact.kind, fact.value)
        if probes.overdue:  # level: re-asserted every turn until an answer or a run change
            with self._lock:
                self._kill_due = KillDue(probes.run, probes.unanswered_ms)
        if self._channel is not None and self._channel.run == probes.run:
            nonce = secrets.token_hex(32)
            if self._send(encode_node_probe(nonce)):
                probes.sent(nonce, now)

    def _receive(self, now: int) -> None:
        channel = self._channel
        try:
            raw = channel.connection.recv(MAX_NODE_LINK_BYTES + 1)
        except BlockingIOError:
            return
        except OSError:
            self._close("error")
            return
        if not raw:
            self._close("eof")
            return
        try:
            nonce = parse_node_probe_answer(raw)
        except ValueError:
            self._close("protocol")  # anything but `probe_answer` ends the channel
            return
        probes = self._probes
        if probes is not None and probes.run == channel.run and probes.answered(nonce, now):
            with self._lock:  # the run answered: a latch set before the answer is stale
                if self._kill_due is not None and self._kill_due.run == channel.run:
                    self._kill_due = None
            self.feed.append("probe_answered", {"run": channel.run.document(),
                                                "rtt_ms": probes.last_rtt_ms})

    def _send(self, packet: bytes) -> bool:
        try:
            return self._channel.connection.send(packet, socket.MSG_DONTWAIT) == len(packet)
        except BlockingIOError:
            return False  # the Player is not reading: this probe goes unsent and unanswered
        except OSError:
            self._close("error")
            return False

    def _close(self, reason: str) -> None:
        channel, self._channel = self._channel, None
        if channel is None:
            return
        try:
            self._selector.unregister(channel.connection)
        except (KeyError, ValueError):
            pass
        channel.connection.close()
        self.feed.append("probe_channel", {"run": channel.run.document(), "state": "closed"})
        LOG.info("broker: probe channel closed (%s)", reason)


__all__ = ["ProbeThread"]
