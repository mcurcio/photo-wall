"""The Pi's display power controller: works each Output toward its Output document and reports back
(roadmap 1b; run ledger .claude/runs/display-1b.md, slice D2; design "Display identity and
power", rule 2 and the PARKED apply rule). Roadmap 2a (Power and the week): the document carries
the port's power timetable, read at the Pi's own wall clock (appliance/display_host/power_timetable.py),
with 1b's console test on top.

Pure core, effects at the edges. `in_force` and `next_deadline` are pure functions of the document,
the Pi's own test start and its two clocks. `OutputPowerController` applies them through three
ports: `PowerMethodAdapter` (one per method; appliance/display_host/power_methods.py),
`BlackPicture` (the shell's black curtain; appliance/display_host/weston.py) and `ReportSink` (the
display bus session; appliance/display_host/bus.py). The controller never blocks
the Weston dispatch loop: it runs its adapters on its own worker thread, one Output at a time, and
every adapter call is bounded by its `timeout`.

The rules it owns (one home each):
- **In force:** the console test while its own count runs, else the timetable at the Pi's wall
  clock. The test starts counting on the Pi's monotonic clock when the Pi first applies it (per
  request id, so a re-put of the same test in a new bus epoch does not restart a running count);
  it has ended when `for_seconds` have passed since then. No document = nothing in force = leave
  the display as it is; past the timetable's `until`, the display is also left as it is
  (TIMETABLE_RAN_OUT).
- **When to act:** once at start, on the display (re)appearing on the Output, when the test ends,
  when the wall clock reaches a set time whose power differs from the one carried out (SET_TIME;
  every tick re-reads it, so a wall-clock step is seen within a tick), and on a change-number rise
  only when the new document wants something else at the Pi's clock (`acts_on_new_document`).
  A rise that wants the same moves the report's `for_change` and keeps its in-force and result.
  Never between those, so it never fights a person with the remote.
- **Black picture** (#83; an opaque black from Display Host, never the Player and never a layer):
  with Darkness.BLACK_PICTURE a timetable off shows the shell's black curtain over the Output and
  runs no power method (the display stays on); its on takes the curtain down. The result is
  CONFIRMED when the shell confirmed the curtain, DID_NOT_ANSWER when it did not (a claim about
  the shell, never about pixels). With Darkness.DISPLAY_OFF, an off no method can carry out
  (NOT_SUPPORTED) shows black instead and keeps NOT_SUPPORTED, so the Power tab still says the
  display cannot be switched (console design §7: "showing black otherwise"). Every other look
  takes a curtain still up down, so black is up exactly while one of those offs is carried out.
- **How to act:** read first and write only when the display reads other than wanted. A display
  that reads as wanted gives `confirmed` with no write; after a DDC/CI standby blip the display
  reappears, reads standby, and is left alone: no loop.
- **Best detected:** a document method of `best-detected` resolves to the first method of
  `METHOD_PRECEDENCE` that answered the read-only check (`probe`); none answered gives
  `not-supported`.
- **#118:** with `never_off_on_other_input`, an off is refused with `another-input` when the
  method says the display shows another input; when it cannot tell (None), the off goes ahead.
- **#117:** with `switch_input_on_power_on`, an on is followed by `claim_input` (its failure does
  not change the power result).
- Never DDC/CI "hard off" (VCP D6 = 5); never a retry loop: one attempt per trigger.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Protocol

from appliance.display_host.power_timetable import acts_on_new_document, next_set_time, wanted
from contracts.node_output import (
    BEST_DETECTED,
    METHOD_PRECEDENCE,
    Darkness,
    DisplayIdentity,
    DisplayMode,
    InForce,
    InForceKind,
    MethodChoice,
    OutputDocument,
    OutputReport,
    Power,
    PowerAttempt,
    PowerMethod,
    PowerResult,
)

PROBE_SECONDS: Final = 5.0     # one read-only check of one method on one Output
READ_SECONDS: Final = 5.0      # one read-back
WRITE_SECONDS: Final = 10.0    # one power write, its read-back included
TICK_SECONDS: Final = 1.0      # the worker's longest sleep: a test end or a set time is seen within it

log = logging.getLogger(__name__)


class Clock(Protocol):
    def monotonic(self) -> float: ...   # the test's count; the Pi's own clock, never compared elsewhere

    def time(self) -> float: ...        # the Pi's wall clock, UTC seconds; compared only with set times

    def wait(self, condition: threading.Condition, seconds: float) -> None:
        """The worker's sleep: wait on `condition` (held) for at most `seconds` of this clock."""
        ...


class PiClock:
    """The Pi's own clocks and a real wait (the production Clock)."""

    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()

    def wait(self, condition: threading.Condition, seconds: float) -> None:
        condition.wait(seconds)


class PowerMethodAdapter(Protocol):
    """One power method on this Pi. Every call returns within its `timeout` (a tool that hangs is
    killed and reads as no answer) and never raises for a display that does not answer."""

    @property
    def method(self) -> PowerMethod: ...

    def probe(self, output_id: str, *, timeout: float) -> bool:
        """Whether the display on `output_id` answers this method's read-only check (CEC: give
        device power status; DDC/CI: getvcp D6; signal off: the compositor has the Output). Reads
        only, never writes."""
        ...

    def read(self, output_id: str, *, timeout: float) -> Power | None:
        """The display's power as this method reads it; None when it gives no answer. Signal off
        reads the compositor's own output power, which says nothing about the panel."""
        ...

    def set(self, output_id: str, power: Power, *, timeout: float) -> PowerResult:
        """Write `power` and read it back: CONFIRMED when the read-back matches, SIGNAL_STOPPED for
        signal off once the compositor did it, DID_NOT_ANSWER otherwise. Never D6 = 5."""
        ...

    def showing_other_input(self, output_id: str, *, timeout: float) -> bool | None:
        """Whether the display shows another input than this Pi's (#118); None when it cannot tell."""
        ...

    def claim_input(self, output_id: str, *, timeout: float) -> bool:
        """Ask the display to show this Pi's input (#117); whether it acknowledged."""
        ...


class BlackPicture(Protocol):
    """The shell's opaque black curtain above every view on an Output (WestonBackend in production).
    Every call returns within its `timeout` and never raises."""

    def show(self, output_id: str, black: bool, *, timeout: float) -> bool:
        """Put the curtain up (`black`) or take it down; whether the shell confirmed it."""
        ...

    def shown(self, output_id: str) -> bool | None:
        """Whether the shell last said the curtain is up; None while it has no such Output."""
        ...


class ReportSink(Protocol):
    """Where the controller's results go: the display bus session in production."""

    def put_report(self, report: OutputReport) -> None: ...   # the Output's latest report; never blocks

    def emit_attempt(self, attempt: PowerAttempt) -> None: ...  # one `display.record.power` event; never blocks


class Trigger(StrEnum):
    """Why the controller looked at an Output."""
    START = "start"
    CHANGE = "change"
    APPEARED = "appeared"
    ENDED = "ended"
    SET_TIME = "set-time"


@dataclass(frozen=True)
class Decision:
    """What one look at an Output decided: what is in force (None: no document) and the power to
    carry out (None: leave the display as it is: no document, or the timetable ran out)."""
    in_force: InForce | None
    power: Power | None


def in_force(document: OutputDocument | None, started: Mapping[str, float], monotonic_now: float,
             wall_now: float) -> Decision:
    """The test while its own count runs (a test absent from `started` has not started, so it is
    running), else `power_timetable.wanted` at the Pi's wall clock. `started` maps the test's id to
    the monotonic second the Pi first applied it. Pure."""
    if document is None:
        return Decision(None, None)
    test = document.power.test
    if test is not None:
        start = started.get(test.request_id)
        left = test.for_seconds if start is None else start + test.for_seconds - monotonic_now
        if left > 0:
            remaining = min(test.for_seconds, math.ceil(left))
            return Decision(InForce(InForceKind.TEST, test.power, test.request_id, remaining, None), test.power)
    timetable = wanted(document.power, wall_now)
    return Decision(InForce(timetable.kind, timetable.power, None, None, timetable.since), timetable.power)


def next_deadline(document: OutputDocument | None, started: Mapping[str, float], monotonic_now: float,
                  wall_now: float) -> float | None:
    """Seconds until the running test ends or the timetable's next set time, whichever is first
    (None: neither). The worker sleeps min(TICK_SECONDS, this). Pure."""
    if document is None:
        return None
    waits: list[float] = []
    test = document.power.test
    if test is not None and (start := started.get(test.request_id)) is not None:
        left = start + test.for_seconds - monotonic_now
        if left > 0:
            waits.append(left)
    following = next_set_time(document.power, wall_now)
    if following is not None:
        waits.append(following - wall_now)
    return min(waits, default=None)


def _carried(in_force: InForce | None) -> tuple[object, ...] | None:
    """What a look carried out, for comparing with what is wanted now: the kind, the power and the
    test's id (never the count or `since`)."""
    return None if in_force is None else (in_force.kind, in_force.power, in_force.request_id)


def _resolve(choice: MethodChoice, answers: tuple[PowerMethod, ...]) -> PowerMethod | None:
    """The method a document's choice names on this display: best detected is the first that
    answered (`answers` keeps METHOD_PRECEDENCE order); a named method only if it answered."""
    if choice == BEST_DETECTED:
        return answers[0] if answers else None
    return choice if choice in answers else None


@dataclass
class _Port:
    """One Output as the controller knows it; guarded by the controller's lock."""
    connected: bool = False
    identity: DisplayIdentity | None = None
    modes: tuple[DisplayMode, ...] = ()
    size_mm: tuple[int, int] | None = None
    document: OutputDocument | None = None
    started: dict[str, float] = field(default_factory=dict)   # the test's id -> first apply
    acted: tuple[object, ...] | None = None   # `_carried` of the last look's in-force
    answers: tuple[PowerMethod, ...] = ()
    method: PowerMethod | None = None
    for_change: int | None = None
    result: PowerResult | None = None
    in_force: InForce | None = None


class OutputPowerController:
    """Every Output's power, from its document, through the adapters, to the sink."""

    def __init__(self, adapters: Mapping[PowerMethod, PowerMethodAdapter], sink: ReportSink, clock: Clock,
                 black: BlackPicture) -> None:
        self._adapters = dict(adapters)
        self._black = black
        self._sink = sink
        self._clock = clock
        self._ports: dict[str, _Port] = {}
        self._due: dict[str, Trigger] = {}        # Outputs waiting for a look, oldest first
        self._reports: dict[str, OutputReport] = {}
        self._wake = threading.Condition()
        self._stopping = False
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        """Start the worker thread; returns at once."""
        with self._wake:
            for output_id in self._ports:
                self._queue(output_id, Trigger.START)
            self._worker = threading.Thread(target=self._run, name="output-power", daemon=True)
            self._worker.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._wake:
            self._stopping = True
            self._wake.notify_all()
        if self._worker is not None:
            self._worker.join(timeout)

    def document(self, output_id: str, document: OutputDocument) -> None:
        """A document for `output_id` arrived (the desired view's callback); acts when its change
        rose and it wants something else at the Pi's clock. Thread-safe, never blocks."""
        if document.output_id != output_id:
            raise ValueError("output_document_output")
        with self._wake:
            port = self._ports.setdefault(output_id, _Port())
            seen = port.document
            port.document = document
            # A count belongs to its request id: a re-put (a new bus epoch) keeps it running.
            test = document.power.test
            port.started = {test.request_id: port.started[test.request_id]} if (
                test is not None and test.request_id in port.started) else {}
            if seen is not None and document.change <= seen.change:
                return
            if seen is None or port.acted is None or acts_on_new_document(
                    seen.power, document.power, self._clock.time()):
                self._queue(output_id, Trigger.CHANGE)
            elif port.for_change is not None:
                # The same wanted power: answer the new change with what is already carried out.
                port.for_change = document.change
                self._put(output_id, port)

    def output(self, output_id: str, *, connected: bool, identity: DisplayIdentity | None,
               modes: tuple[DisplayMode, ...], size_mm: tuple[int, int] | None = None) -> None:
        """What the compositor and the EDID say about `output_id` now (`size_mm`: the EDID's picture
        size, None when it states none); a disconnected-then-connected Output is APPEARED. Re-puts the Output's report. Thread-safe, never blocks."""
        with self._wake:
            first = output_id not in self._ports
            port = self._ports.setdefault(output_id, _Port())
            appeared = connected and not port.connected
            port.connected, port.identity, port.modes, port.size_mm = connected, identity, modes, size_mm
            if first:
                self._queue(output_id, Trigger.START)
            elif appeared:
                self._queue(output_id, Trigger.APPEARED)
            self._put(output_id, port)

    def report(self, output_id: str) -> OutputReport | None:
        """The Output's latest report as last put (None before the first)."""
        with self._wake:
            return self._reports.get(output_id)

    # -- the worker: one look at a time, never under the lock ------------------------------

    def _queue(self, output_id: str, trigger: Trigger) -> None:
        """Under the lock: one pending look per Output (a look reads the latest document)."""
        self._due.setdefault(output_id, trigger)
        self._wake.notify_all()

    def _put(self, output_id: str, port: _Port) -> None:
        """Under the lock: the Output's report, put only when it differs from the last."""
        report = OutputReport(output_id, port.connected, port.identity, port.modes, port.answers,
                              port.method, port.for_change, port.result, port.in_force, port.size_mm)
        if self._reports.get(output_id) != report:
            self._reports[output_id] = report
            self._sink.put_report(report)

    def _run(self) -> None:
        while True:
            with self._wake:
                while not self._stopping and not self._due:
                    self._clock.wait(self._wake, self._sleep())
                    self._ended()
                if self._stopping:
                    return
                output_id = next(iter(self._due))
                trigger = self._due.pop(output_id)
            try:
                self._act(output_id, trigger)
            except Exception:
                log.exception("output power: %s look at %s failed", trigger.value, output_id)

    def _sleep(self) -> float:
        """Under the lock: until the next test end or set time, never past TICK_SECONDS."""
        monotonic_now, wall_now = self._clock.monotonic(), self._clock.time()
        waits = [wait for port in self._ports.values()
                 if (wait := next_deadline(port.document, port.started, monotonic_now, wall_now)) is not None
                 and wait > 0]
        return min([TICK_SECONDS, *waits])

    def _ended(self) -> None:
        """Under the lock: an Output whose in-force is no longer the one it carried out (the test
        ended, or the wall clock reached a set time with another power)."""
        monotonic_now, wall_now = self._clock.monotonic(), self._clock.time()
        for output_id, port in self._ports.items():
            if port.acted is None:
                continue
            current = _carried(in_force(port.document, port.started, monotonic_now, wall_now).in_force)
            if current is not None and current != port.acted:
                ended = port.acted[0] is InForceKind.TEST
                self._queue(output_id, Trigger.ENDED if ended else Trigger.SET_TIME)

    def _act(self, output_id: str, trigger: Trigger) -> None:
        """One look at one Output: probe every method (reads only), then carry out the request in
        force. One attempt, never a retry. A disconnected Output is still looked at: some displays
        drop hot-plug for the whole of standby and still answer DDC/CI or CEC."""
        began = self._clock.monotonic()
        answers = tuple(method for method in METHOD_PRECEDENCE if method in self._adapters
                        and self._adapters[method].probe(output_id, timeout=PROBE_SECONDS))
        with self._wake:
            port = self._ports.setdefault(output_id, _Port())
            document, now = port.document, self._clock.monotonic()
            # This look reads the latest document, so a change that arrived while it probed is
            # carried out here: its pending look would be a second attempt for the same change.
            # (A pending APPEARED stays: the display came back after this look's probes.)
            if self._due.get(output_id) is Trigger.CHANGE:
                del self._due[output_id]
            decision = in_force(document, port.started, now, self._clock.time())
            carried, power = decision.in_force, decision.power
            if document is None or carried is None or power is None:   # leave the display as it is
                port.answers = answers
                if port.method not in answers:
                    port.method = None
                if document is not None:          # ran out: say so for this change; no attempt
                    port.for_change, port.in_force, port.acted = document.change, carried, _carried(carried)
                self._put(output_id, port)
                return
            if carried.kind is InForceKind.TEST and carried.request_id is not None:
                port.started.setdefault(carried.request_id, now)
        method = _resolve(document.method, answers)
        black_picture = (carried.kind is InForceKind.TIMETABLE
                         and document.power.darkness is Darkness.BLACK_PICTURE)
        if black_picture and power is Power.OFF:
            # Black picture's off: the curtain is the whole of it; no power method runs.
            confirmed = self._black.show(output_id, True, timeout=WRITE_SECONDS)
            result = PowerResult.CONFIRMED if confirmed else PowerResult.DID_NOT_ANSWER
        elif black_picture and method is None:
            # Its on with no method to check the display: the curtain comes down, and that is all.
            confirmed = self._black.show(output_id, False, timeout=WRITE_SECONDS)
            result = PowerResult.CONFIRMED if confirmed else PowerResult.DID_NOT_ANSWER
        elif method is None:
            result = PowerResult.DID_NOT_ANSWER if answers else PowerResult.NOT_SUPPORTED
            black = power is Power.OFF and result is PowerResult.NOT_SUPPORTED   # showing black otherwise
            if self._curtain(output_id, black) is False and black:
                result = PowerResult.DID_NOT_ANSWER      # nothing turned it off: the photos stay up
        else:
            # Every on, black picture's too, brings the display itself on (read first, write only
            # when it reads off): a test, or a night with displays turned off, may have left it off.
            lowered = (self._black.show(output_id, False, timeout=WRITE_SECONDS) if black_picture
                       else self._curtain(output_id, False))
            result = _carry_out(self._adapters[method], output_id, power, document)
            if lowered is False:
                result = PowerResult.DID_NOT_ANSWER      # the display is on; the shell kept black up
        elapsed_ms = max(0, round((self._clock.monotonic() - began) * 1000))
        with self._wake:
            port.answers, port.method = answers, method
            port.for_change, port.result = document.change, result
            port.in_force, port.acted = carried, _carried(carried)
            self._put(output_id, port)
        self._sink.emit_attempt(PowerAttempt(output_id, document.change, carried, method, result, elapsed_ms))

    def _curtain(self, output_id: str, black: bool) -> bool | None:
        """Up when `black`; else down when the shell may have it up (it said so, or cannot say).
        Whether the shell confirmed; None when nothing was asked of it."""
        if black or self._black.shown(output_id) is not False:
            return self._black.show(output_id, black, timeout=WRITE_SECONDS)
        return None


def _carry_out(adapter: PowerMethodAdapter, output_id: str, power: Power,
               document: OutputDocument) -> PowerResult:
    """Read first; write only when the display reads other than `power`."""
    if (power is Power.OFF and document.never_off_on_other_input
            and adapter.showing_other_input(output_id, timeout=READ_SECONDS) is True):
        return PowerResult.ANOTHER_INPUT
    if adapter.read(output_id, timeout=READ_SECONDS) is power:
        return PowerResult.SIGNAL_STOPPED if adapter.method is PowerMethod.SIGNAL_OFF else PowerResult.CONFIRMED
    result = adapter.set(output_id, power, timeout=WRITE_SECONDS)
    # #117 only after a write: an input write may bounce hot-plug, and a bounce is a new look.
    if (power is Power.ON and document.switch_input_on_power_on
            and result in (PowerResult.CONFIRMED, PowerResult.SIGNAL_STOPPED)):
        adapter.claim_input(output_id, timeout=WRITE_SECONDS)
    return result


__all__ = ["PROBE_SECONDS", "READ_SECONDS", "TICK_SECONDS", "WRITE_SECONDS", "BlackPicture", "Clock", "Decision",
           "OutputPowerController", "PiClock", "PowerMethodAdapter", "ReportSink", "Trigger", "in_force", "next_deadline"]
