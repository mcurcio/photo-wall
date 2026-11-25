"""What the everyday rules read, one Frame at a time, and the stores' protocols (decision 0036).
Every fact is judged by its owner before it gets here: Frame status says whether a Frame is set
up, answering, resting or paused (central/frame_status/policy.py `everyday_facts`), Coordination
which layers its Player's newest offer holds and what each shows."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from central.everyday.model import Cover, SkipOutcome, SkipScope
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
class FrameFacts:
    """One Frame as the everyday rules read it, in one transaction.

    `layers`: the media and black layers of its Player's newest valid offer on it not yet over;
    `origins` what made each (by assignment id; a layer with none is no Intent's now). `recent`:
    its covers of earlier skips, newest first (a Previous chain). `history`: its shown history,
    newest first. `listed`: the Never show list's photos."""
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


__all__ = ["FrameFacts", "FrameFactsReader", "LayerOrigin", "NeverShowList", "ShownPhoto", "SkipRecord",
           "Skips"]
