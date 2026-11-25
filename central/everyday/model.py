"""The everyday actions' vocabulary (decision 0036, "Slice 3 design"). Pure.

Central sends codes; every string a person reads is the console's, worded from the generated
contract (central/console_contract.py). Each vocabulary here is closed. Instants are Central's
clock (seconds since the epoch).
"""
from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal

from pydantic import Field

from contracts.identifiers import Identifier
from contracts.models import AfterEnd, Instant, Layer, Model

# Placeholders to tune on the bench (decision 0036): how far ahead of a request its Frames move
# together (the plan must reach the Pis first), how far ahead a hand-made end is cut, and when an
# action not yet seen on its Frame is late.
SKIP_LEAD: Final = 2.0
END_LEAD: Final = 2.0
ACK_LATE_SECONDS: Final = 30.0
# How long a pause lasts when the request names no length: one hour (setting #86).
PAUSE_SECONDS: Final = 3600.0
# The longest pause a request may ask for: a day.
PAUSE_MAX_SECONDS: Final = 86400.0


class SkipKind(StrEnum):
    """What a skip asks: the Frame's next photo, or the one it showed before."""
    NEXT = "next"
    PREVIOUS = "previous"


class SkipScope(StrEnum):
    """Where a skip is pressed: on a wall (every Frame of it, the owner's S1) or on one Frame; a
    photo for Never show's skip of every Frame showing it."""
    WALL = "wall"
    FRAME = "frame"
    PHOTO = "photo"


class CoverCause(StrEnum):
    """Why a cover was added: a Skip, a Previous, or a photo put on the Never show list."""
    NEXT = "next"
    PREVIOUS = "previous"
    NEVER_SHOW = "never-show"


class StayReason(StrEnum):
    """Why a Frame stays when an action is asked, closed; first match wins (policy.py `stays`)."""
    NOT_SET_UP = "not-set-up"
    NOT_ANSWERING = "not-answering"
    RESTING = "resting"
    PAUSED = "paused"
    NOTHING_TO_SKIP = "nothing-to-skip"
    BLACK = "black"
    HAND_PLACED = "hand-placed"
    NO_PREVIOUS = "no-previous"


class PauseRefusal(StrEnum):
    """Why a Frame is not paused when Pause is asked, closed; first match wins (policy.py
    `pause_refusal`). No refusal for a photo due on another Frame (the owner's S4)."""
    NOT_SET_UP = "not-set-up"
    NOT_ANSWERING = "not-answering"
    RESTING = "resting"
    PAUSED = "paused"                    # already paused: its banner offers Carry on
    NOTHING_TO_HOLD = "nothing-to-hold"  # no media layer drawn, or a black one or an ending
    VIDEO = "video"                      # the owner's S8: no Pause on a video


class EverydayRefusal(StrEnum):
    """Why an everyday action is refused, closed (each answered with its HTTP status by
    central/everyday_routes.py)."""
    INVALID_SKIP = "invalid_operator_skip"      # 422: the body does not read
    INVALID_NEVER_SHOW = "invalid_never_show"   # 422
    UNKNOWN_WALL = "unknown_wall"               # 404
    UNKNOWN_FRAME = "unknown_frame"             # 404
    UNKNOWN_PHOTO = "unknown_photo"             # 404: the library holds no such photo
    NOTHING_MOVED = "nothing-moved"             # 409, with `stayed`: no Frame could move
    CHANGED = "changed"                         # 409: the Frame's photo turned over under the thumb
    INVALID_PAUSE = "invalid_operator_pause"    # 422
    UNKNOWN_PAUSE = "unknown_pause"             # 404: no such pause holds that Frame
    NOTHING_PAUSED = "nothing-paused"           # 409, with `stayed`: no Frame could be paused
    INVALID_SHOW_NOW = "invalid_show_now"       # 422: Show now's or Back to normal's body


class AskedAction(StrEnum):
    """An action asked of a Frame whose effect is acknowledged (`Asked`)."""
    SKIP = "skip"
    PREVIOUS = "previous"
    NEVER_SHOW = "never-show"
    PAUSE = "pause"
    CARRY_ON = "carry-on"
    BACK_TO_NORMAL = "back-to-normal"


class AskState(StrEnum):
    """`requested` until the Player reports the action's layer presented, then `done`; `late`
    when not done by `at + ACK_LATE_SECONDS`."""
    REQUESTED = "requested"
    DONE = "done"
    LATE = "late"


class Asked(Model):
    """The newest action asked of a Frame and where it stands."""
    action: AskedAction
    request_id: Identifier
    at: Instant
    state: AskState


CoverHow = Literal["joins", "pick", "pinned"]


class Cover(Model):
    """One Frame's cover as the command decided it: a layer added above the Frame's layers from
    `start` (the shared instant) to `end`, never a change to an offered layer.

    - `joins`: the Frame's own next photo, `layer` built whole (it ends where that photo's own
      layer is fully in, so the show goes on with no seam);
    - `pick`: a slot the wall plan fills from the covered layer's Scene line, never `left`;
    - `pinned`: a slot the wall plan fills with `pinned` alone (Previous).

    `covered` is the layer it covers; `joined` the layer a `joins` cover gives way to; `left`
    the photo it leaves; `cue` the coordination group's
    root a wall's covers share (they await a grant and start whole before `start` wherever
    every Pi is ready, decision 0031), None for a Frame's own. `anchor` and `steps` chain a
    Previous (policy.py `previous_step`)."""
    cover_id: Identifier
    frame_id: Identifier
    cause: CoverCause
    how: CoverHow
    covered: Identifier
    run_id: Identifier
    output_id: Identifier
    binding_generation: int = Field(ge=1)
    priority: int = 0
    root_order: int = Field(default=0, ge=0)
    admission_order: int = Field(default=0, ge=0)
    start: Instant
    end: Instant
    fade_in: float = Field(default=0, ge=0)
    fade_out: float = Field(default=0, ge=0)
    after_end: AfterEnd = "leave_as_is"
    cue: Identifier | None = None
    left: Identifier | None = None
    layer: Layer | None = None
    joined: Identifier | None = None
    pinned: Identifier | None = None
    anchor: Identifier | None = None
    steps: int = Field(default=0, ge=0)

    @property
    def awaits_grant(self) -> bool:
        return self.cue is not None


class Stayed(Model):
    frame_id: Identifier
    reason: StayReason


class Moved(Model):
    frame_id: Identifier


class SkipOutcome(Model):
    """What one skip did: the shared instant, each Frame's cover and each that stayed."""
    at: Instant
    covers: tuple[Cover, ...] = ()
    stayed: tuple[Stayed, ...] = ()

    @property
    def ends_at(self) -> float:
        """The last cover's end (pruning), or `at` when none moved."""
        return max((cover.end for cover in self.covers), default=self.at)


class Pause(Model):
    """One Frame's pause (decision 0036): the photo it shows held from `started_at` to `ends_at`
    by `held`, a layer added above every Run (`hold_for`); `ended_at` when Carry on ended it early
    (`held` then carries the cut). `pause_id` is the request's: one request may pause several
    Frames. `by`: the 0035 actor. `scene_id`: the held photo's Scene (its shown-history row)."""
    pause_id: Identifier
    frame_id: Identifier
    by: str
    started_at: Instant
    ends_at: Instant
    ended_at: Instant | None = None
    scene_id: Identifier
    held: Layer

    def live(self, now: float) -> bool:
        """Whether its hold is still drawn, or still to be, at `now`: not withdrawn by a Carry on
        before it began (ended with no cut), and not past its drawn end."""
        return (self.ended_at is None or self.held.cut_at is not None) and self.held.drawn_end > now


class PausedFrame(Model):
    frame_id: Identifier


class PauseStayed(Model):
    frame_id: Identifier
    reason: PauseRefusal


__all__ = ["ACK_LATE_SECONDS", "END_LEAD", "PAUSE_MAX_SECONDS", "PAUSE_SECONDS", "SKIP_LEAD", "Pause", "PauseRefusal",
           "PauseStayed", "PausedFrame", "AskState", "Asked", "AskedAction", "Cover",
           "CoverCause", "CoverHow", "EverydayRefusal", "Moved", "SkipKind", "SkipOutcome", "SkipScope", "StayReason",
           "Stayed"]
