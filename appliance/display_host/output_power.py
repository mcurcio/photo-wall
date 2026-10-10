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

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from contracts.node_output import (
    DisplayIdentity,
    DisplayMode,
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
    raise NotImplementedError


def next_deadline(document: OutputDocument | None, started: Mapping[str, float]) -> float | None:
    """The monotonic second the request now in force ends (None: nothing timed is in force). Pure."""
    raise NotImplementedError


class OutputPowerController:
    """Every Output's power, from its document, through the adapters, to the sink."""

    def __init__(self, adapters: Mapping[PowerMethod, PowerMethodAdapter], sink: ReportSink,
                 clock: Clock) -> None:
        raise NotImplementedError

    def start(self) -> None:
        """Start the worker thread; returns at once."""
        raise NotImplementedError

    def stop(self, timeout: float = 5.0) -> None:
        raise NotImplementedError

    def document(self, output_id: str, document: OutputDocument) -> None:
        """A document for `output_id` arrived (the desired view's callback); acts when its change
        rose. Thread-safe, never blocks."""
        raise NotImplementedError

    def output(self, output_id: str, *, connected: bool, identity: DisplayIdentity | None,
               modes: tuple[DisplayMode, ...]) -> None:
        """What the compositor and the EDID say about `output_id` now; a disconnected-then-connected
        Output is APPEARED. Re-puts the Output's report. Thread-safe, never blocks."""
        raise NotImplementedError

    def report(self, output_id: str) -> OutputReport | None:
        """The Output's latest report as last put (None before the first)."""
        raise NotImplementedError


__all__ = ["PROBE_SECONDS", "READ_SECONDS", "WRITE_SECONDS", "Clock", "Decision",
           "OutputPowerController", "PowerMethodAdapter", "ReportSink", "Trigger", "in_force", "next_deadline"]
