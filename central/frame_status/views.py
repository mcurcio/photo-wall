"""Home's document and the Frame page's status shapes (decision 0036, "Home's read"). Pure.

Instants are Central's clock (seconds since the epoch). Every value is a code, an id or a name a
person typed; the console words the codes.
"""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field

from central.frame_status.model import (
    FrameStatus,
    HouseStatus,
    Override,
    ProblemCode,
    Row,
    SetupStep,
    StatusWord,
)
from contracts.identifiers import Identifier
from contracts.models import Instant, Model

# The kinds of request that can win a Frame: central/why/model.py `ContentKind`, restated because
# that module's imports reach central.fleet; tests/test_frame_status_policy.py holds the two equal.
PlayingKind = Literal["program", "show-now", "default-scene"]


class Playing(Model):
    """What plays: the request that wins (a Program's window, the Default Scene or Show now), its
    Scene, its name (the Program's; else the Scene id) and when it ends by itself (None: no end
    known)."""
    kind: PlayingKind
    scene_id: Identifier
    program_id: Identifier | None = None
    name: str
    until: Instant | None = None


class Upcoming(Model):
    """What follows: the Program whose next window starts first, and when."""
    program_id: Identifier
    scene_id: Identifier
    name: str
    at: Instant


class NowNext(Model):
    """One Wall's now and next: what plays on most of its Frames, and what follows on any of them."""
    surface_id: Identifier
    now: Playing | None = None
    next: Upcoming | None = None


class FramePlace(Model):
    """One Frame on its Wall: its name, where it hangs (its top-left, millimetres) and its size as
    it hangs (None: not known), its Pi (its Player's id and device; None: none), what plays on it
    and its status."""
    frame_id: Identifier
    name: str
    x_mm: float
    y_mm: float
    size_mm: tuple[float, float] | None
    player_id: Identifier | None = None
    device_id: Identifier | None = None
    playing: Playing | None = None
    status: FrameStatus


class WallView(Model):
    """One Wall and its Frames, in Frame id order."""
    surface_id: Identifier
    name: str
    room: str
    frames: tuple[FramePlace, ...]


class HomeView(Model):
    """Home's one document. `change` moves when anything else in it does (`home_view`): the
    console asks with `If-None-Match` on it, so an unchanged wall costs no body. Nothing here
    assumes polling: a push stream sends the same document."""
    change: Annotated[int, Field(ge=0)]
    house: HouseStatus
    walls: tuple[WallView, ...]
    overrides: tuple[Override, ...]
    now_next: tuple[NowNext, ...]


def home_view(*, house: HouseStatus, walls: tuple[WallView, ...], overrides: tuple[Override, ...],
              now_next: tuple[NowNext, ...]) -> HomeView:
    """The document with its change number: 48 bits of the SHA-256 of everything else in it, as
    canonical JSON (whole in a JavaScript number), so equal documents carry equal numbers across
    reads, processes and restarts."""
    body = HomeView(change=0, house=house, walls=walls, overrides=overrides, now_next=now_next)
    canonical = json.dumps(body.model_dump(mode="json", exclude={"change"}), sort_keys=True, separators=(",", ":"))
    return body.model_copy(update={"change": int.from_bytes(hashlib.sha256(canonical.encode()).digest()[:6], "big")})


class StatusChangeView(Model):
    """One recorded change of a Frame's status: what it became, since when, and when Central
    recorded it."""
    word: StatusWord | None
    setup: SetupStep | None
    row: Row
    problem: ProblemCode | None
    since: Instant
    recorded_at: Instant


class FrameStatusView(Model):
    """The Frame page's status: the Frame's status now and its recent changes, newest first, from
    Central's event log (kept a week)."""
    status: FrameStatus
    recent: tuple[StatusChangeView, ...]


__all__ = ["FramePlace", "FrameStatusView", "HomeView", "NowNext", "Playing", "PlayingKind", "StatusChangeView", "Upcoming",
           "WallView", "home_view"]
