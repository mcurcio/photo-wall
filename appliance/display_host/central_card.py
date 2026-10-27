"""Central's card: Display Host's controller composes it per Output (DESIGN-r8 §5; BUILD.md: the
Frame shows Central's error when it blacks the Frame). Pure: no I/O, no clock.

The controller holds every fact the rule reads but one (power, its own drawing, the admitted run)
and reads the last, the Player's `impeded` report (contracts/node_impeded.py), on the Pi's bus. The
rule, per Output:

  1. powered off (Night off, Resting)                                     -> none
  2. its own drawing (test pattern, calibration target, trial)            -> none
  3. no Player run admitted (the slate shows)                             -> none
  4. the report names this Output's Frame under the admitted grant        -> Central's card
  5. otherwise (not reported yet, a report it can't read, another grant)  -> none

The card goes to the overlay client by the shell's `central_card` event (frame protocol,
pw_diagnostic_manager_v1 v6), never through the health judge: Central's error never goes on the
health line, and the overlay client draws the health judge's card first, else Central's.

TODO(display lane): the body of `central_card`, the bus reader of the Player's line, the shell op
and its re-sends, the overlay client's drawing (Central's look, labelled "Central").
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from contracts.node_impeded import ImpededFrames
from contracts.node_output import CentralCard


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
    raise NotImplementedError("display lane")


class ImpededReports(Protocol):
    """The controller's one new input: the Player line's IMPEDED_KEY on the Pi's bus, read by
    Display's own session (never the Player's process or screen connection)."""

    def latest(self) -> ImpededFrames | None:
        """The report as last read; None: none read yet since the bus came up, or one that does not
        decode."""
        ...


__all__ = ["ImpededReports", "OutputFacts", "central_card"]
