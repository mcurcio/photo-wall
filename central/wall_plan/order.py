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
  capture month and day on the house calendar equal `today`'s (the house's date, Settings › House),
  newest year first, then the rest in shuffle order.
- **A Scene's pass** (`scene_passes`): from the history, its current pass number (the highest
  among its shown rows and live selections; 1 when it has none), the assets picked in that pass
  (shown in it, or selected in it by a live offered layer), and its last shown asset.
- **Tiers** (`SceneLine.ranked`): (0) not picked in this pass and not shown on any Frame within
  the Scene's no_repeat_days of `now`; (1) not picked in this pass but shown within them;
  (2) picked in this pass. Within a tier, the pass sequence.
- **Rolling a pass** (`SceneLine.pick`): when every `pickable` asset is picked in the current
  pass, the next pass starts: pass number + 1, nothing picked, a new pass sequence.
- **The orders as data** (follow-up S3, `ORDERS`): each order's sort keys, declared once and
  interpreted twice, here (`order_key`) and in SQL (central/infra/photo_library_store.py), so
  the Planner reads only the top of a line (`LineQuery`, `SceneLines`). A line read in part
  holds every member it has not picked when its last member is picked (`SceneLine`'s
  `complete`); only then may its pass end in this plan.

Dates are the house's (roadmap 2b; central/house_time/calculator.py `house_date`, the one home of
an instant's house date), as the photo library's relative dates are (media/pool_check.py).
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final, Literal, Protocol

from pydantic import Field

from central.catalog import Candidate, SourcePool
from central.house_time.calculator import house_date
from central.house_time.model import HouseTime
from central.wall_plan.selection import Order, SceneSelection
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


# --- The order declaration (follow-up S3, design rule 3): one home, two interpreters ----------
# `order_key` interprets it in Python (`pass_sequence`, `PoolLines`);
# central/infra/photo_library_store.py `ranked_line_in` renders it to SQL, so the store returns a
# Scene line's top K. tests/test_order_sql_conformance.py holds the two equal for every order,
# tier and wrap.

# The shuffle's keyed hash: sha256 over these fields' UTF-8 text joined by the separator; digests
# ascending (hex text order here, bytea order in SQL: the same).
SHUFFLE_FIELDS: Final = ("scene_id", "pass_number", "asset_id")
SHUFFLE_SEPARATOR: Final = b"\x00"

OrderField = Literal[
    "shuffle",        # the keyed hash above
    "wrapped",        # chronological's wrap: 1 at or before the mark (the last shown photo)
    "captured_at",
    "asset_id",       # code-point order (SQL: COLLATE "C")
    "off_this_day",   # 0 for a photo whose house capture month and day are today's, else 1
]


@dataclass(frozen=True, slots=True)
class OrderTerm:
    """One sort key of a pass order. `on_this_day_only`: a constant for a photo not taken on
    today's month and day (SQL: NULL), so the key orders the On this day group only."""

    field: OrderField
    descending: bool = False
    on_this_day_only: bool = False


# Each order's pass sequence as sort keys, ascending unless `descending`; each ends unique.
ORDERS: Final[Mapping[Order, tuple[OrderTerm, ...]]] = {
    "shuffle": (OrderTerm("shuffle"),),
    "chronological": (OrderTerm("wrapped"), OrderTerm("captured_at"), OrderTerm("asset_id")),
    "newest_first": (OrderTerm("captured_at", descending=True), OrderTerm("asset_id")),
    "on_this_day": (OrderTerm("off_this_day"),
                    OrderTerm("captured_at", descending=True, on_this_day_only=True),
                    OrderTerm("asset_id", on_this_day_only=True), OrderTerm("shuffle")),
}

# The tiers (module docstring): `SceneLine.ranked`, `PoolLines` and the store's SQL read these.
TIER_FRESH: Final = 0
TIER_RECENT: Final = 1
TIER_PICKED: Final = 2


def tier_of(asset_id: str, picked: frozenset[str] | set[str], recent: frozenset[str]) -> int:
    """An asset's tier: picked in this pass, else shown within the no-repeat window, else fresh."""
    if asset_id in picked:
        return TIER_PICKED
    return TIER_RECENT if asset_id in recent else TIER_FRESH


def recent_assets(shown_at: Mapping[str, float], no_repeat_days: float, *,
                  now: float) -> frozenset[str]:
    """The assets shown on any Frame within `no_repeat_days` of `now` (`last_shown_at`'s ends)."""
    window = no_repeat_days * 86400
    return frozenset(asset_id for asset_id, end in shown_at.items()
                     if window > 0 and end >= now - window)


def shuffle_key(scene_id: str, pass_number: PassNumber, asset_id: str) -> str:
    values = {"scene_id": scene_id, "pass_number": str(pass_number), "asset_id": asset_id}
    return hashlib.sha256(SHUFFLE_SEPARATOR.join(
        values[name].encode() for name in SHUFFLE_FIELDS)).hexdigest()


# The house a caller that names none is in (tests of the order alone); the Planner always passes
# Runtime's house.
UTC_HOUSE: Final = HouseTime(zone="UTC")


def on_day(captured_at: float, today: date, house: HouseTime = UTC_HOUSE) -> bool:
    """Whether a photo's capture month and day on the house calendar are `today`'s."""
    taken = house_date(house, captured_at)
    return (taken.month, taken.day) == (today.month, today.day)


def order_key(order: Order, *, scene_id: str, pass_number: PassNumber,
              mark: tuple[float, str] | None, today: date,
              house: HouseTime = UTC_HOUSE) -> Callable[[Candidate], tuple]:
    """The Python interpreter of `ORDERS[order]`: one candidate's sort key. `mark`:
    (captured_at, asset_id) of the Scene's last shown photo, chronological's wrap point."""
    terms = ORDERS[order]

    def key(candidate: Candidate) -> tuple:
        today_photo = on_day(candidate.captured_at, today, house)
        values: list = []
        for term in terms:
            if term.on_this_day_only and not today_photo:
                values.append("" if term.field == "asset_id" else 0)
                continue
            value: float | str
            if term.field == "shuffle":
                value = shuffle_key(scene_id, pass_number, candidate.asset_id)
            elif term.field == "wrapped":
                value = int(mark is not None
                            and (candidate.captured_at, candidate.asset_id) <= mark)
            elif term.field == "captured_at":
                value = candidate.captured_at
            elif term.field == "asset_id":
                value = candidate.asset_id
            else:
                value = 0 if today_photo else 1
            if term.descending:
                if isinstance(value, str):
                    raise AssertionError("a text key is never descending")
                value = -value
            values.append(value)
        return tuple(values)

    return key


def pass_sequence(candidates: Sequence[Candidate], selection: SceneSelection, *, scene_id: str,
                  pass_number: PassNumber, after: Candidate | None, today: date,
                  house: HouseTime = UTC_HOUSE) -> tuple[str, ...]:
    """Every candidate's asset id once, in the Scene's order for this pass (module docstring),
    by `order_key`. `after` is used by chronological only; `today` and `house` by On this day.
    Pure and deterministic."""
    unique = list({candidate.asset_id: candidate for candidate in reversed(candidates)}.values())
    mark = (after.captured_at, after.asset_id) if after is not None else None
    key = order_key(selection.order, scene_id=scene_id, pass_number=pass_number, mark=mark,
                    today=today, house=house)
    return tuple(candidate.asset_id for candidate in sorted(unique, key=key))


# --- What the Planner reads of a Scene's line (follow-up S3) -----------------------------------


class ShapeClass(Model):
    """The photos one class of a Scene's slots could take, judged by revision size alone: ratio
    (width / height) inside [min_ratio, max_ratio], a video only where `video`, an image at least
    `min_width` x `min_height` (its revision's; its readable size is never larger). A superset of
    what the exact fit rule accepts (central/wall_plan/assignment.py `shape_bounds`)."""

    video: bool
    min_ratio: float = Field(gt=0, allow_inf_nan=False)
    max_ratio: float = Field(gt=0, allow_inf_nan=False)
    min_width: int = Field(default=0, ge=0)
    min_height: int = Field(default=0, ge=0)


class ShapeBounds(Model):
    """A Specification: a line member passes when any class admits it."""

    classes: tuple[ShapeClass, ...] = ()

    def admits(self, kind: str, width: int, height: int) -> bool:
        ratio = width / height
        return any(
            shape.min_ratio <= ratio <= shape.max_ratio
            and (shape.video if kind == "video"
                 else width >= shape.min_width and height >= shape.min_height)
            for shape in self.classes)


class LineQuery(Model):
    """One read of a Scene's line: the members of `pool_keys` that `shape` admits, ranked by tier
    (`tier_of` over `picked` and `recent`) then the pass order (`ORDERS[order]` for `scene_id`
    and `pass_number`; the wrap mark is `after`, looked up in the line; On this day's date is
    `now`'s date on `house`'s calendar), the first `limit`. Capture time and size are read at
    query time."""

    pool_keys: tuple[str, ...]
    scene_id: str = Field(min_length=1, max_length=160)
    pass_number: PassNumber = Field(ge=1)
    order: Order
    after: str | None = None
    now: Instant
    picked: frozenset[str] = frozenset()
    recent: frozenset[str] = frozenset()
    shape: ShapeBounds
    limit: int = Field(ge=1)
    house: HouseTime = UTC_HOUSE

    @property
    def today(self) -> date:
        return house_date(self.house, self.now)


class LineMember(Model):
    """A hydrated line member and the query's pool keys that hold it."""

    candidate: Candidate
    pools: frozenset[str]


class LineAnswer(Model):
    """`SceneLines.line`'s answer: the ranked members, and the wrap mark's photo (the query's
    `after`, when the line holds it, whether the shape admits it or not)."""

    members: tuple[LineMember, ...] = ()
    after: Candidate | None = None


class SourceLine(Model):
    """A saved Source revision as the Planner reads it: its pool key (None: no readable query or
    no pool yet), whether the pool has a complete check, and whether it holds no photo."""

    source_ref: str
    pool_key: str | None
    ready: bool
    empty: bool


class SceneLines(Protocol):
    """What the Planner reads of the photo library each pass: central/media_repository.py
    `PgSceneLines` in coordination's transaction, `PoolLines` in memory."""

    def sources(self, source_refs: set[str]) -> dict[str, SourceLine]:
        """Each named Source revision; a ref naming none is left out (`source_missing`)."""
        ...

    def line(self, query: LineQuery) -> LineAnswer:
        """`query`'s members, hydrated (variant, preparation failure, key content, readable
        size)."""
        ...


class PoolLines:
    """`SceneLines` over hydrated in-memory pools keyed by source ref (each ref its own pool
    key): the Python interpreter of `LineQuery`, for tests and the SQL conformance test."""

    def __init__(self, pools: Mapping[str, SourcePool]) -> None:
        self._pools = dict(pools)

    def sources(self, source_refs: set[str]) -> dict[str, SourceLine]:
        return {ref: SourceLine(source_ref=pool.source_ref, pool_key=ref, ready=pool.ready,
                                empty=not pool.candidates)
                for ref in source_refs if (pool := self._pools.get(ref)) is not None}

    def line(self, query: LineQuery) -> LineAnswer:
        members: dict[str, Candidate] = {}
        pools: dict[str, set[str]] = {}
        for key in query.pool_keys:
            pool = self._pools.get(key)
            for candidate in pool.candidates if pool is not None else ():
                members.setdefault(candidate.asset_id, candidate)
                pools.setdefault(candidate.asset_id, set()).add(key)
        after = members.get(query.after) if query.after is not None else None
        mark = (after.captured_at, after.asset_id) if after is not None else None
        order = order_key(query.order, scene_id=query.scene_id, pass_number=query.pass_number,
                          mark=mark, today=query.today, house=query.house)
        admitted = [candidate for candidate in members.values() if query.shape.admits(
            candidate.kind, candidate.original_width, candidate.original_height)]
        admitted.sort(key=lambda c: (tier_of(c.asset_id, query.picked, query.recent), order(c)))
        return LineAnswer(members=tuple(
            LineMember(candidate=candidate, pools=frozenset(pools[candidate.asset_id]))
            for candidate in admitted[:query.limit]), after=after)


class SceneLine:
    """One Scene's photos in the order its Frames take them, for one plan. Mutable only through
    `pick` within one planning call; built fresh on every call."""

    def __init__(self, scene_id: str, selection: SceneSelection, candidates: Sequence[Candidate],
                 scene_pass: ScenePass, shown_at: Mapping[str, float], *, now: float, today: date,
                 pickable: frozenset[str], whole: bool = True, complete: bool | None = None,
                 mark: Candidate | None = None, house: HouseTime = UTC_HOUSE) -> None:
        """`candidates`: the Scene's line members the Planner read (one per asset id): the whole
        union of its contributions' pools when `whole`, else the top of it (follow-up S3).
        `complete` (default `whole`): every member not picked in the current pass is among
        `candidates`, so the pass may end in this plan; after it ends, a partial line's next
        pass is never complete. `mark`: the Scene's last shown photo when it is not among
        `candidates` (chronological's wrap point). `shown_at`: `last_shown_at(history)`.
        `pickable`: the assets that could fill one of the Scene's slots in this plan (they fit a
        Frame the Scene targets and are measured); a pass ends when all of them are picked.
        ValueError when `pickable` names an asset not in `candidates`."""
        self._candidates = {candidate.asset_id: candidate for candidate in reversed(candidates)}
        unknown = pickable - self._candidates.keys()
        if unknown:
            raise ValueError(f"pickable assets not in the line: {sorted(unknown)}")
        self._scene_id = scene_id
        self._selection = selection
        self._pickable = pickable
        self._whole = whole
        self._complete = whole if complete is None else (whole or complete)
        last = scene_pass.last_shown
        self._after = None if last is None else self._candidates.get(last) or (
            mark if mark is not None and mark.asset_id == last else None)
        self._today, self._house = today, house
        self._recent = recent_assets(shown_at, selection.no_repeat_days, now=now)
        self._pass = scene_pass.pass_number
        self._picked = set(scene_pass.picked)
        self._sequence: tuple[str, ...] = ()
        # A pass the history already completed (its last pick ended the previous plan) is over.
        if not self._roll():
            self._sequence = self._pass_sequence()

    def _pass_sequence(self) -> tuple[str, ...]:
        return pass_sequence(tuple(self._candidates.values()), self._selection,
                             scene_id=self._scene_id, pass_number=self._pass, after=self._after,
                             today=self._today, house=self._house)

    def _roll(self) -> bool:
        if not self._complete or not self._pickable or not self._pickable <= self._picked:
            return False
        self._pass += 1
        self._picked = set()
        self._complete = self._whole
        self._sequence = self._pass_sequence()
        return True

    @property
    def pass_number(self) -> PassNumber:
        return self._pass

    def ranked(self) -> tuple[str, ...]:
        """Every candidate's asset id, tiers first, then the pass sequence (module docstring)."""
        position = {asset_id: index for index, asset_id in enumerate(self._sequence)}
        return tuple(sorted(self._sequence, key=lambda a: (
            tier_of(a, self._picked, self._recent), position[a])))

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
