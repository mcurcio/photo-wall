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

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime

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
    rows = (*history.shown, *history.selected)
    current: dict[str, PassNumber] = {}
    for row in rows:
        current[row.scene_id] = max(current.get(row.scene_id, 1), row.pass_number)
    picked: dict[str, set[str]] = {scene_id: set() for scene_id in current}
    for row in rows:
        if row.pass_number == current[row.scene_id]:
            picked[row.scene_id].add(row.asset_id)
    last: dict[str, PhotoShowing] = {}
    for row in history.shown:
        held = last.get(row.scene_id)
        if held is None or (row.start, row.assignment_id) > (held.start, held.assignment_id):
            last[row.scene_id] = row
    return {
        scene_id: ScenePass(
            scene_id=scene_id, pass_number=pass_number, picked=frozenset(picked[scene_id]),
            last_shown=last[scene_id].asset_id if scene_id in last else None,
        )
        for scene_id, pass_number in current.items()
    }


def last_shown_at(history: WallHistory) -> dict[str, float]:
    """The latest shown `end` of each asset, on any Frame and any Scene. Pure."""
    latest: dict[str, float] = {}
    for row in history.shown:
        latest[row.asset_id] = max(latest.get(row.asset_id, row.end), row.end)
    return latest


def _shuffle_key(scene_id: str, pass_number: PassNumber, asset_id: str) -> str:
    return hashlib.sha256(f"{scene_id}\x00{pass_number}\x00{asset_id}".encode()).hexdigest()


def _captured(candidate: Candidate) -> datetime:
    return datetime.fromtimestamp(candidate.captured_at, UTC)


def pass_sequence(candidates: Sequence[Candidate], selection: SceneSelection, *, scene_id: str,
                  pass_number: PassNumber, after: Candidate | None, today: date) -> tuple[str, ...]:
    """Every candidate's asset id once, in the Scene's order for this pass (module docstring).
    `after` is used by chronological only. Pure and deterministic."""
    unique = list({candidate.asset_id: candidate for candidate in reversed(candidates)}.values())

    def shuffled(items: Sequence[Candidate]) -> list[Candidate]:
        return sorted(items, key=lambda c: _shuffle_key(scene_id, pass_number, c.asset_id))

    if selection.order == "shuffle":
        ordered = shuffled(unique)
    elif selection.order == "chronological":
        ordered = sorted(unique, key=lambda c: (c.captured_at, c.asset_id))
        if after is not None:
            mark = (after.captured_at, after.asset_id)
            later = [c for c in ordered if (c.captured_at, c.asset_id) > mark]
            ordered = later + [c for c in ordered if (c.captured_at, c.asset_id) <= mark]
    elif selection.order == "newest_first":
        ordered = sorted(unique, key=lambda c: (-c.captured_at, c.asset_id))
    else:
        def on_this_day(candidate: Candidate) -> bool:
            taken = _captured(candidate)
            return (taken.month, taken.day) == (today.month, today.day)

        ordered = sorted((c for c in unique if on_this_day(c)),
                         key=lambda c: (-c.captured_at, c.asset_id))
        ordered += shuffled([c for c in unique if not on_this_day(c)])
    return tuple(candidate.asset_id for candidate in ordered)


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
        self._candidates = {candidate.asset_id: candidate for candidate in reversed(candidates)}
        unknown = pickable - self._candidates.keys()
        if unknown:
            raise ValueError(f"pickable assets not in the line: {sorted(unknown)}")
        self._scene_id = scene_id
        self._selection = selection
        self._pickable = pickable
        self._after = (self._candidates.get(scene_pass.last_shown)
                       if scene_pass.last_shown is not None else None)
        self._today = today
        window = selection.no_repeat_days * 86400
        self._recent = frozenset(
            asset_id for asset_id, end in shown_at.items()
            if window > 0 and end >= now - window
        )
        self._pass = scene_pass.pass_number
        self._picked = set(scene_pass.picked)
        self._sequence: tuple[str, ...] = ()
        # A pass the history already completed (its last pick ended the previous plan) is over.
        if not self._roll():
            self._sequence = self._pass_sequence()

    def _pass_sequence(self) -> tuple[str, ...]:
        return pass_sequence(tuple(self._candidates.values()), self._selection,
                             scene_id=self._scene_id, pass_number=self._pass, after=self._after,
                             today=self._today)

    def _roll(self) -> bool:
        if not self._pickable or not self._pickable <= self._picked:
            return False
        self._pass += 1
        self._picked = set()
        self._sequence = self._pass_sequence()
        return True

    @property
    def pass_number(self) -> PassNumber:
        return self._pass

    def ranked(self) -> tuple[str, ...]:
        """Every candidate's asset id, tiers first, then the pass sequence (module docstring)."""
        def tier(asset_id: str) -> int:
            if asset_id in self._picked:
                return 2
            return 1 if asset_id in self._recent else 0

        position = {asset_id: index for index, asset_id in enumerate(self._sequence)}
        return tuple(sorted(self._sequence, key=lambda a: (tier(a), position[a])))

    def pick(self, asset_id: str) -> PassNumber:
        """Record that a slot of this Scene took `asset_id` in the current pass; return that pass
        number. Rolls to the next pass once every pickable asset is picked (after recording).
        Picking an asset already picked in this pass (a repeat) is allowed and changes nothing
        but the return. KeyError for an asset not in the line."""
        if asset_id not in self._candidates:
            raise KeyError(asset_id)
        picked_in = self._pass
        self._picked.add(asset_id)
        self._roll()
        return picked_in
