"""The everyday actions' request and answer shapes (decision 0036, "Slice 3 design"), served by
central/everyday_routes.py and generated into the console's contract. Pure."""
from __future__ import annotations

from typing import Literal

from central.everyday.model import Moved, SkipKind, Stayed
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


__all__ = ["LastShowing", "NeverShowAnswer", "NeverShowEntry", "NeverShowListView", "NeverShowRequest",
           "SkipAnswer", "SkipRequest"]
