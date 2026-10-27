"""Central's card: Display Host's controller composes it per Output (DESIGN-r8 §5; BUILD.md: the
Frame shows Central's error when it blacks the Frame). No I/O and no clock.

The controller holds every fact the rule reads but one (power, its own drawing, the admitted run)
and reads the last, the Player's `impeded` report (contracts/node_impeded.py), on the Pi's bus. The
rule, per Output (`central_card`):

  1. powered off (Night off, Resting)                                     -> none
  2. its own drawing (test pattern, calibration target, trial)            -> none
  3. no Player run admitted (the slate shows)                             -> none
  4. the report names this Output's Frame under the admitted grant        -> Central's card
  5. otherwise (not reported yet, a report it can't read, another grant)  -> none

Step 4 is identity, never time: the report names the shell grant the Frame was drawn under, and an
entry counts only while that grant is the one admitted on the Output. A Player crash or hang ends the
admission (the shell invalidates the grant, at the latest when its lease lapses), so an ended run's
report never keeps the card up, and a new run must restate it (R4).

`CentralCards` is where the facts meet: each owner pushes its own fact when it changes (the main
loop the admission and trial, the power controller's reports the power, the picture controller's
shell op the drawing, the bus reader the report), and a changed card goes to `send` (the runner's
shell forwarder, which never blocks). The card goes to the overlay client by the shell's
`central_card` event (frame protocol, pw_diagnostic_manager_v1 v6), never through the health judge:
Central's error never goes on the health line, and the overlay client draws the health judge's card
first, else Central's.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from contracts.node_impeded import ImpededFrames
from contracts.node_output import OUTPUT_IDS, CentralCard


@dataclass(frozen=True)
class OutputFacts:
    """What the controller holds about one Output when it composes the card."""
    output_id: str
    powered: bool                   # False: Night off or Resting (the Output document's power now)
    own_drawing: bool               # a test pattern, calibration target or trial is Display's own drawing
    admitted_grant: str | None      # the grant id of the admitted Player surface; None: none admitted (the slate)
    admitted_frame: str | None      # that surface's Frame id; None: none admitted


def central_card(output: OutputFacts, impeded: ImpededFrames | None) -> CentralCard | None:
    """The rule above. `impeded` is the Player line's report as last read (None: never reported, or
    a value the controller could not decode). An entry counts only when its output_id, frame_id and
    grant_id are the Output's and its admitted surface's."""
    if (not output.powered or output.own_drawing or output.admitted_grant is None
            or output.admitted_frame is None or impeded is None or output.output_id not in OUTPUT_IDS):
        return None
    for entry in impeded.frames:
        if (entry.output_id, entry.frame_id, entry.grant_id) == (
                output.output_id, output.admitted_frame, output.admitted_grant):
            return CentralCard(output.output_id, entry.words.line1, entry.words.line2)
    return None


class ImpededReports(Protocol):
    """The controller's one new input: the Player line's IMPEDED_KEY on the Pi's bus, read by
    Display's own session (never the Player's process or screen connection)."""

    def latest(self) -> ImpededFrames | None:
        """The report as last read; None: none read yet since the bus came up, or one that does not
        decode."""
        ...


@dataclass(frozen=True)
class _Held:
    """One Output's facts as their owners last pushed them. An Output no owner has spoken of is
    powered (no report yet means nothing is off), not drawing, and has no run admitted."""
    powered: bool = True
    pattern: bool = False                 # the picture op's `drawing`: a test pattern or target
    trial: bool = False                   # a live calibration trial
    grant: str | None = None
    frame: str | None = None

    def facts(self, output_id: str) -> OutputFacts:
        return OutputFacts(output_id, self.powered, self.pattern or self.trial, self.grant, self.frame)


class CentralCards:
    """The controller's Central cards: the facts per Output, composed by `central_card` on every
    change, a changed card handed to `send(output_id, card)` (None: no card). Thread-safe: each
    owner pushes from its own thread; `send` must never block (it is called under the lock, so the
    order of sends is the order of changes)."""

    def __init__(self, reports: ImpededReports, send: Callable[[str, CentralCard | None], None]) -> None:
        self._reports, self._send = reports, send
        self._lock = threading.Lock()
        self._held: dict[str, _Held] = {}
        self._cards: dict[str, CentralCard | None] = {}

    def admitted(self, output_id: str, grant: str | None, frame: str | None, *, trial: bool) -> None:
        """The main loop's facts: the admitted run's grant and Frame (None: none, the slate) and
        whether a live trial is Display's drawing on the Output."""
        self._push(output_id, grant=grant, frame=frame, trial=trial)

    def power(self, output_id: str, powered: bool) -> None:
        """The power controller's report: whether the power in force leaves the display on."""
        self._push(output_id, powered=powered)

    def drawing(self, output_id: str, drawing: bool) -> None:
        """The picture controller's shell op: whether Display draws a test pattern or target."""
        self._push(output_id, pattern=drawing)

    def reported(self) -> None:
        """The Player's report changed: every Output is composed again."""
        with self._lock:
            for output_id in self._held:
                self._compose(output_id)

    def card(self, output_id: str) -> CentralCard | None:
        """The Output's card as last composed."""
        with self._lock:
            return self._cards.get(output_id)

    def _push(self, output_id: str, **facts: object) -> None:
        with self._lock:
            held = self._held.get(output_id, _Held())
            changed = replace(held, **facts)
            self._held[output_id] = changed
            if changed != held or output_id not in self._cards:
                self._compose(output_id)

    def _compose(self, output_id: str) -> None:
        card = central_card(self._held[output_id].facts(output_id), self._reports.latest())
        if output_id in self._cards and self._cards[output_id] == card:
            return
        self._cards[output_id] = card
        self._send(output_id, card)


__all__ = ["CentralCards", "ImpededReports", "OutputFacts", "central_card"]
