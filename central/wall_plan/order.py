"""Each Scene's line of photos, in its order, across passes (roadmap 5a; run ledger
.claude/runs/photo-5a.md, slice O1). Pure: the shown history is an input, the date is supplied.

Owner, 2026-10-11: "“Shown” means it was displayed on a frame. Also note that this “no repeat” idea
is secondary to always ensuring that frames have content". So every rule here only RANKS; nothing
here removes a photo from a line, and a repeat beats an empty Frame.

Rules this module owns (one home each):
- **The pass sequence** (`pass_sequence`): every candidate once, in the Scene's order. Shuffle:
  ascending sha256 of "<scene_id>\\x00<pass_number>\\x00<asset_id>" (a keyed hash: a refresh that
  adds or removes photos keeps the others' relative order; a new pass reshuffles). Chronological:
  ascending (captured_at, asset_id), starting after `after` (the Scene's last shown photo) and
  wrapping. Newest first: descending captured_at, then asset_id. On this day: the photos whose
  UTC capture month and day equal `today`'s, newest year first, then the rest in shuffle order.
- **A Scene's pass** (`scene_passes`): from the history, its current pass number (the highest
  among its shown rows and live selections; 1 when it has none), the assets picked in that pass
  (shown in it, or selected in it by a live offered layer), and its last shown asset.
- **Tiers** (`SceneLine.ranked`): (0) not picked in this pass and not shown on any Frame within
  the Scene's no_repeat_days of `now`; (1) not picked in this pass but shown within them;
  (2) picked in this pass. Within a tier, the pass sequence.
- **Rolling a pass** (`SceneLine.pick`): when every `pickable` asset is picked in the current
  pass, the next pass starts: pass number + 1, nothing picked, a new pass sequence.

Dates are UTC until roadmap 2b gives the wall a time zone (the photo library's relative dates do
the same, central/photo_library/model.py).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from pydantic import Field

from central.catalog import Candidate
from central.wall_plan.selection import SceneSelection
from contracts.models import AfterEnd, Identifier, Instant, Model

PassNumber = int


class PhotoShowing(Model):
    """One photo's planned showing on one Frame: what an offered layer shows (recorded when it is
    first offered; its content is locked from then on), and, once its Player reports it
    presented, a shown row (design `ShownEntry`). Timed by the layer's planned interval on
    Central's clock; the Player sends "presented" only for visible layers, so a covered layer is
    never shown."""

    assignment_id: Identifier
    asset_id: Identifier
    frame_id: Identifier
    scene_id: str = Field(min_length=1, max_length=160)
    pass_number: PassNumber = Field(ge=1)
    start: Instant
    end: Instant
    fade_in: float = Field(ge=0, allow_inf_nan=False)
    after_end: AfterEnd


class KeptStill(Model):
    """The photo a Frame keeps now: its latest shown layer that set what it keeps, when that layer
    said "keep_this_photo" (a "keep_nothing" one leaves nothing kept). `since` is when it took
    effect (contracts/on_screen.py `kept_from`)."""

    frame_id: Identifier
    asset_id: Identifier
    since: Instant


class WallHistory(Model):
    """What the Planner reads of the past (central/infra/shown_history_store.py
    `wall_history_in`): shown rows within the longest no-repeat window plus every row of each
    Scene's current pass; the live selections; each Frame's kept still."""

    shown: tuple[PhotoShowing, ...] = ()        # presented
    selected: tuple[PhotoShowing, ...] = ()     # offered, ending after now
    kept: tuple[KeptStill, ...] = ()


class ScenePass(Model):
    scene_id: str
    pass_number: PassNumber = Field(default=1, ge=1)
    picked: frozenset[str] = frozenset()
    last_shown: str | None = None


def scene_passes(history: WallHistory) -> dict[str, ScenePass]:
    """Each Scene's pass as the module docstring says, for every Scene the history names. A Scene
    absent from the result starts at pass 1 with nothing picked. Pure."""
    raise NotImplementedError


def last_shown_at(history: WallHistory) -> dict[str, float]:
    """The latest shown `end` of each asset, on any Frame and any Scene. Pure."""
    raise NotImplementedError


def pass_sequence(candidates: Sequence[Candidate], selection: SceneSelection, *, scene_id: str,
                  pass_number: PassNumber, after: Candidate | None, today: date) -> tuple[str, ...]:
    """Every candidate's asset id once, in the Scene's order for this pass (module docstring).
    `after` is used by chronological only. Pure and deterministic."""
    raise NotImplementedError


class SceneLine:
    """One Scene's photos in the order its Frames take them, for one plan. Mutable only through
    `pick` within one planning call; built fresh on every call."""

    def __init__(self, scene_id: str, selection: SceneSelection, candidates: Sequence[Candidate],
                 scene_pass: ScenePass, shown_at: Mapping[str, float], *, now: float, today: date,
                 pickable: frozenset[str]) -> None:
        """`candidates`: the union of the Scene's contributions' pools (one per asset id).
        `shown_at`: `last_shown_at(history)`. `pickable`: the assets that could fill one of the
        Scene's slots in this plan (they fit a Frame the Scene targets and are measured); a pass
        ends when all of them are picked. ValueError when `pickable` names an asset not in
        `candidates`."""
        raise NotImplementedError

    @property
    def pass_number(self) -> PassNumber:
        raise NotImplementedError

    def ranked(self) -> tuple[str, ...]:
        """Every candidate's asset id, tiers first, then the pass sequence (module docstring)."""
        raise NotImplementedError

    def pick(self, asset_id: str) -> PassNumber:
        """Record that a slot of this Scene took `asset_id` in the current pass; return that pass
        number. Rolls to the next pass once every pickable asset is picked (after recording).
        Picking an asset already picked in this pass (a repeat) is allowed and changes nothing
        but the return. KeyError for an asset not in the line."""
        raise NotImplementedError
