"""What the everyday rules read, one Frame at a time, and the stores' protocols (decision 0036).
Every fact is judged by its owner before it gets here: Frame status says whether a Frame is set
up, answering, resting or paused (central/frame_status/policy.py `everyday_facts`), Coordination
which layers its Player's newest offer holds and what each shows."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from central.everyday.model import AskedAction, Cover, SkipOutcome, SkipScope
from contracts.models import Layer


@dataclass(frozen=True)
class LayerOrigin:
    """What made an offered layer: its Runtime Intent's Scene, whether its photo is hand-placed
    (the Intent names `asset_refs`) or a Scene's ending (its phase is `outro`), and its Scene's
    Transition seconds (a cover's fade-in)."""
    scene_id: str
    hand_placed: bool = False
    ending: bool = False
    transition: float = 0.0


@dataclass(frozen=True)
class ShownPhoto:
    """One photo a Frame's Player reported presented (shown history), newest first in `history`."""
    asset_id: str
    assignment_id: str
    presented_at: float


@dataclass(frozen=True)
class AskFact:
    """The newest action asked of a Frame that moved it, and when its layer (`cover_id`) was
    reported presented (None: not yet). `until`: when it stops being reported
    (central/everyday/policy.py `asked_until`)."""
    action: AskedAction
    request_id: str
    at: float
    until: float
    presented_at: float | None = None


@dataclass(frozen=True)
class Showing:
    """What a Frame shows and has shown, as Coordination and shown history hold it (each field as
    `FrameFacts` says); Frame status adds its own judgements (central/frame_status/policy.py
    `everyday_facts`)."""
    layers: tuple[Layer, ...] = ()
    origins: Mapping[str, LayerOrigin] = field(default_factory=dict)
    recent: tuple[Cover, ...] = ()
    history: tuple[ShownPhoto, ...] = ()
    listed: frozenset[str] = frozenset()
    asked: AskFact | None = None
    # When each cover of `recent` was reported presented (by cover id; absent: not yet).
    presented: Mapping[str, float] = field(default_factory=dict)
    # Whether a photo of `history` may still be stepped back onto (`FrameFacts.usable`).
    usable: Callable[[str], bool] | None = None


@dataclass(frozen=True)
class FrameFacts:
    """One Frame as the everyday rules read it, in one transaction.

    `layers`: the media and black layers of its Player's newest valid offer on it not yet over;
    `origins` what made each (by assignment id; a layer with none is no Intent's now). `recent`:
    its covers of earlier skips, newest first (a Previous chain). `history`: its shown history,
    newest first. `listed`: the Never show list's photos. `usable`: whether a photo of its history
    may still be stepped back onto (still in the library and fitting the Frame as the Scene it
    shows judges it), judged by its store, the one judge Home's `can` and the command both read
    (decision 0036, the independent review); None: any photo (a test's facts)."""
    frame_id: str
    set_up: bool = True
    answering: bool = True
    resting: bool = False
    paused: bool = False
    layers: tuple[Layer, ...] = ()
    origins: Mapping[str, LayerOrigin] = field(default_factory=dict)
    recent: tuple[Cover, ...] = ()
    history: tuple[ShownPhoto, ...] = ()
    listed: frozenset[str] = frozenset()
    usable: Callable[[str], bool] | None = None


def frame_facts(frame_id: str, showing: Showing, *, set_up: bool, answering: bool, resting: bool,
                paused: bool, unseen: frozenset[str] = frozenset()) -> FrameFacts:
    """One Frame's facts from what it shows and its owners' judgements. `unseen`: the covers its
    Pi never showed in time (central/everyday/policy.py `unseen_covers`), which are not what the
    Frame shows: they are left out of its layers and its covers, so a press after one (Retry)
    asks the same step again, never one further."""
    return FrameFacts(frame_id=frame_id, set_up=set_up, answering=answering, resting=resting,
                      paused=paused,
                      layers=tuple(layer for layer in showing.layers if layer.assignment_id not in unseen),
                      origins=showing.origins,
                      recent=tuple(cover for cover in showing.recent if cover.cover_id not in unseen),
                      history=showing.history, listed=showing.listed, usable=showing.usable)


class UsableReader(Protocol):
    """Previous's judge of a Frame's history photos, read in the caller's transaction: whether each
    is still in the library and still fits the Frame `frame_id` as a Scene of `selection` judges it
    (`Showing.usable`). `selection` is that Scene's own (central.wall_plan.selection's
    `SceneSelection`, opaque here: the everyday rules never read it)."""

    def __call__(self, conn: Any, frame_id: str, selection: Any) -> Callable[[str], bool]: ...


class FrameFactsReader(Protocol):
    """Frame id -> its `FrameFacts`, read in the caller's transaction at Central's `now`, for
    `frame_ids` (None: every Frame)."""

    def __call__(self, conn: Any, now: float, frame_ids: Sequence[str] | None = None,
                 ) -> Mapping[str, FrameFacts]: ...


@dataclass(frozen=True)
class SkipRecord:
    """One skip as kept (central/infra/skip_store.py): its request, where, what and what it did."""
    request_id: str
    scope: SkipScope
    target_id: str
    cause: str
    outcome: SkipOutcome
    by: str


class Skips(Protocol):
    """The `skips` table's repository, called in Coordination's transaction."""

    def get_in(self, conn: Any, request_id: str) -> SkipRecord | None: ...

    def add_in(self, conn: Any, record: SkipRecord, *, now: float) -> None: ...

    def live_in(self, conn: Any, now: float) -> tuple[Cover, ...]: ...

    def recent_in(self, conn: Any, frame_ids: Sequence[str], now: float,
                  ) -> Mapping[str, tuple[tuple[SkipRecord, Cover | None], ...]]: ...


class NeverShowList(Protocol):
    """The Never show list's repository (central/infra/never_show_store.py)."""

    def listed_in(self, conn: Any) -> frozenset[str]: ...


__all__ = ["AskFact", "FrameFacts", "FrameFactsReader", "LayerOrigin", "NeverShowList", "Showing", "ShownPhoto",
           "SkipRecord", "Skips", "UsableReader", "frame_facts"]
