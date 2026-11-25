"""The everyday actions' request and answer shapes (decision 0036, "Slice 3 design"), served by
central/everyday_routes.py and generated into the console's contract. Pure."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from central.everyday.model import (
    PAUSE_MAX_SECONDS,
    PAUSE_SECONDS,
    Moved,
    PausedFrame,
    PauseStayed,
    SkipKind,
    Stayed,
)
from contracts.identifiers import Identifier
from contracts.models import Instant, Model


class SkipRequest(Model):
    """POST walls/{surface_id}/skips and frames/{frame_id}/skips: one request id per press (a
    double tap is one request), and which way."""
    request_id: Identifier
    kind: SkipKind = SkipKind.NEXT


class SkipAnswer(Model):
    """What a skip did: the shared instant the Frames move at, each Frame that moved and each that
    stayed with why. `duplicate`: the request was asked before, and this is its stored answer."""
    request_id: Identifier
    kind: SkipKind
    at: Instant
    duplicate: bool = False
    moved: tuple[Moved, ...] = ()
    stayed: tuple[Stayed, ...] = ()


class NeverShowRequest(Model):
    """POST never-show: the photo (its `asset_id` as a Frame's showing names it) and, from a
    Frame's menu, the Frame it was seen on (answered 409 `changed` when that Frame shows another
    photo now: it turned over under the thumb)."""
    request_id: Identifier
    asset_id: Identifier
    frame_id: Identifier | None = None


class LastShowing(Model):
    """A showing of a listed photo already downloaded for another Frame's slot: it shows there
    that one last time (the owner's S5; the download lock stays)."""
    frame_id: Identifier
    start: Instant
    until: Instant


class NeverShowAnswer(Model):
    asset_id: Identifier
    duplicate: bool = False
    skipped_on: tuple[Identifier, ...] = ()
    stayed: tuple[Stayed, ...] = ()
    last_showings: tuple[LastShowing, ...] = ()


class NeverShowEntry(Model):
    """One listed photo (Scenes › Never show list), by whom and when."""
    asset_id: Identifier
    kind: Literal["image", "video"]
    listed_at: Instant
    by: str


class NeverShowListView(Model):
    photos: tuple[NeverShowEntry, ...] = ()


class PauseRequest(Model):
    """POST pauses: one request id per press (a double tap is one pause), the Frames, and how
    long (seconds; default one hour, setting #86)."""
    request_id: Identifier
    frame_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=64)
    seconds: float = Field(default=PAUSE_SECONDS, gt=0, le=PAUSE_MAX_SECONDS)


class PauseAnswer(Model):
    """What a pause did: its id (the request's), when it ends, each Frame paused and each that
    stayed with why. `duplicate`: asked before; this is what it paused."""
    pause_id: Identifier
    ends_at: Instant
    duplicate: bool = False
    paused: tuple[PausedFrame, ...] = ()
    stayed: tuple[PauseStayed, ...] = ()


class CarryOnAnswer(Model):
    """DELETE pauses/{pause_id} (every Frame of it) or pauses/{pause_id}/frames/{frame_id}: the
    Frames it ended and when their holds are gone (`until`, the fade's end)."""
    pause_id: Identifier
    ended: tuple[PausedFrame, ...] = ()
    until: Instant


class EndsIn(Model):
    """Show now ends this long after it starts (setting #84's default, one hour, is the sheet's)."""
    kind: Literal["in"] = "in"
    seconds: float = Field(gt=0, le=7 * 86400)


class EndsAtNightOff(Model):
    """Show now ends at tonight's Night off (its next start within a day)."""
    kind: Literal["night-off"] = "night-off"


class EndsWhenStopped(Model):
    """Show now plays until Back to normal is pressed."""
    kind: Literal["stopped"] = "stopped"


ShowNowEnds = Annotated[EndsIn | EndsAtNightOff | EndsWhenStopped, Field(discriminator="kind")]


class BackToNormalRequest(Model):
    """POST runs/{run_id}/end: one request id per press."""
    request_id: Identifier


class BackToNormalAnswer(Model):
    """Back to normal: the takeover's Run, when its fade begins (`at`) and when it is gone
    (`until`); what is beneath shows then at its current state."""
    run_id: Identifier
    request_id: Identifier
    at: Instant
    until: Instant


__all__ = ["BackToNormalAnswer", "BackToNormalRequest", "CarryOnAnswer", "EndsAtNightOff", "EndsIn",
           "EndsWhenStopped", "PauseAnswer", "PauseRequest", "ShowNowEnds", "LastShowing", "NeverShowAnswer", "NeverShowEntry", "NeverShowListView", "NeverShowRequest",
           "SkipAnswer", "SkipRequest"]
