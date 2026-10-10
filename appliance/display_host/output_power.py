"""The Pi's display power controller: works each Output toward its Output document and reports back
(roadmap 1b; run ledger .claude/runs/display-1b.md, slice D2; design "Display identity and
power", rule 2 and the PARKED apply rule).

Pure core, effects at the edges. `in_force` and `decide` are pure functions of the document, the
Pi's own timer starts and its monotonic clock. `OutputPowerController` applies them through two
ports: `PowerMethodAdapter` (one per method; appliance/display_host/power_methods.py) and
`ReportSink` (the display bus session; appliance/display_host/bus.py). The controller never blocks
the Weston dispatch loop: it runs its adapters on its own worker thread, one Output at a time, and
every adapter call is bounded by its `timeout`.

The rules it owns (one home each):
- **In force:** the first request of the document's stack that has not ended. A timed request
  starts counting on the Pi's monotonic clock when the Pi first applies it (per request id, so a
  re-put of the same request in a new bus epoch does not restart a running count); it has ended
  when `for_seconds` have passed since then. No document = nothing in force = leave the display
  as it is.
- **When to act:** on a change-number rise, on the display (re)appearing on the Output, when a
  timed request ends, and once at start. Never between those, so it never fights a person with
  the remote.
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
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Protocol

from contracts.node_output import (
    BEST_DETECTED,
    METHOD_PRECEDENCE,
    DisplayIdentity,
    DisplayMode,
    InForce,
    MethodChoice,
    OutputDocument,
    OutputReport,
    Power,
    PowerAttempt,
    PowerMethod,
    PowerRequest,
    PowerResult,
)

PROBE_SECONDS: Final = 5.0     # one read-only check of one method on one Output
READ_SECONDS: Final = 5.0      # one read-back
WRITE_SECONDS: Final = 10.0    # one power write, its read-back included
TICK_SECONDS: Final = 1.0      # the worker's longest sleep: a timed request ends within it

log = logging.getLogger(__name__)


class Clock(Protocol):
    def monotonic(self) -> float: ...                 # seconds; the Pi's own clock, never compared elsewhere


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


class ReportSink(Protocol):
    """Where the controller's results go: the display bus session in production."""

    def put_report(self, report: OutputReport) -> None: ...   # the Output's latest report; never blocks

    def emit_attempt(self, attempt: PowerAttempt) -> None: ...  # one `display.record.power` event; never blocks


class Trigger(StrEnum):
    """Why the controller looked at an Output (the apply rule's four triggers)."""
    START = "start"
    CHANGE = "change"
    APPEARED = "appeared"
    ENDED = "ended"


@dataclass(frozen=True)
class Decision:
    """What one look at an Output decided: the request in force (None = no document: leave it) and
    the Pi's remaining count for it."""
    in_force: PowerRequest | None
    remaining_seconds: int | None


def in_force(document: OutputDocument | None, started: Mapping[str, float], now: float) -> Decision:
    """The first request of `document.power` that has not ended at `now`; `started` maps a timed
    request's id to the monotonic second the Pi first applied it (a timed request absent from it
    has not started, so it has not ended). Pure."""
    if document is None:
        return Decision(None, None)
    for request in document.power:
        if request.for_seconds is None:
            return Decision(request, None)
        start = started.get(request.request_id)
        if start is None:
            return Decision(request, request.for_seconds)
        left = start + request.for_seconds - now
        if left > 0:
            return Decision(request, min(request.for_seconds, math.ceil(left)))
    return Decision(None, None)     # unreachable for a valid document: its last request is untimed


def next_deadline(document: OutputDocument | None, started: Mapping[str, float]) -> float | None:
    """The monotonic second the request now in force ends (None: nothing timed is in force). Pure.

    It takes no clock reading, so it is the earliest end among the started timed requests above
    the stack's first untimed or not-yet-started request: in a 1b stack (at most one timed request
    over the standing one) exactly the request in force. The worker never sleeps past
    TICK_SECONDS, so a deeper stack still ends within a tick."""
    if document is None:
        return None
    ends: list[float] = []
    for request in document.power:
        start = started.get(request.request_id)
        if request.for_seconds is None or start is None:
            break
        ends.append(start + request.for_seconds)
    return min(ends, default=None)


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
    document: OutputDocument | None = None
    started: dict[str, float] = field(default_factory=dict)   # timed request id -> first apply
    acted: str | None = None                  # the request id the last attempt carried out
    answers: tuple[PowerMethod, ...] = ()
    method: PowerMethod | None = None
    for_change: int | None = None
    result: PowerResult | None = None
    in_force: InForce | None = None


class OutputPowerController:
    """Every Output's power, from its document, through the adapters, to the sink."""

    def __init__(self, adapters: Mapping[PowerMethod, PowerMethodAdapter], sink: ReportSink,
                 clock: Clock) -> None:
        self._adapters = dict(adapters)
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
        rose. Thread-safe, never blocks."""
        if document.output_id != output_id:
            raise ValueError("output_document_output")
        with self._wake:
            port = self._ports.setdefault(output_id, _Port())
            seen = port.document
            port.document = document
            # A count belongs to its request id: a re-put (a new bus epoch) keeps it running.
            port.started = {request.request_id: port.started[request.request_id]
                            for request in document.power if request.request_id in port.started}
            if seen is None or document.change > seen.change:
                self._queue(output_id, Trigger.CHANGE)

    def output(self, output_id: str, *, connected: bool, identity: DisplayIdentity | None,
               modes: tuple[DisplayMode, ...]) -> None:
        """What the compositor and the EDID say about `output_id` now; a disconnected-then-connected
        Output is APPEARED. Re-puts the Output's report. Thread-safe, never blocks."""
        with self._wake:
            first = output_id not in self._ports
            port = self._ports.setdefault(output_id, _Port())
            appeared = connected and not port.connected
            port.connected, port.identity, port.modes = connected, identity, modes
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
                              port.method, port.for_change, port.result, port.in_force)
        if self._reports.get(output_id) != report:
            self._reports[output_id] = report
            self._sink.put_report(report)

    def _run(self) -> None:
        while True:
            with self._wake:
                while not self._stopping and not self._due:
                    self._wake.wait(self._sleep())
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
        """Under the lock: until the next timed request ends, never past TICK_SECONDS."""
        now = self._clock.monotonic()
        waits = [deadline - now for port in self._ports.values()
                 if (deadline := next_deadline(port.document, port.started)) is not None and deadline > now]
        return min([TICK_SECONDS, *waits])

    def _ended(self) -> None:
        """Under the lock: an Output whose request in force is no longer the one it carried out."""
        now = self._clock.monotonic()
        for output_id, port in self._ports.items():
            if port.acted is None:
                continue
            current = in_force(port.document, port.started, now).in_force
            if current is not None and current.request_id != port.acted:
                self._queue(output_id, Trigger.ENDED)

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
            decision = in_force(document, port.started, now)
            request = decision.in_force
            if document is None or request is None:   # no document: leave the display as it is
                port.answers = answers
                if port.method not in answers:
                    port.method = None
                self._put(output_id, port)
                return
            if request.for_seconds is not None:
                port.started.setdefault(request.request_id, now)
        method = _resolve(document.method, answers)
        if method is None:
            result = PowerResult.DID_NOT_ANSWER if answers else PowerResult.NOT_SUPPORTED
        else:
            result = _carry_out(self._adapters[method], output_id, request.power, document)
        elapsed_ms = max(0, round((self._clock.monotonic() - began) * 1000))
        with self._wake:
            port.answers, port.method = answers, method
            port.for_change, port.result = document.change, result
            port.in_force = InForce(request.request_id, decision.remaining_seconds)
            port.acted = request.request_id
            self._put(output_id, port)
        self._sink.emit_attempt(PowerAttempt(output_id, document.change, request.request_id,
                                             request.power, method, result, elapsed_ms))


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


__all__ = ["PROBE_SECONDS", "READ_SECONDS", "TICK_SECONDS", "WRITE_SECONDS", "Clock", "Decision",
           "OutputPowerController", "PowerMethodAdapter", "ReportSink", "Trigger", "in_force", "next_deadline"]
