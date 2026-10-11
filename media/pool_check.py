"""The pure fold of one pool check (roadmap 4, run ledger .claude/runs/photo-4.md slice M2).

No I/O and no clock reads: `media.library_checks.LibraryChecks` reads Immich and the store and
hands this module what they answered. A check runs in phases, one bounded step each:

1. **up_to**: Immich's statistics for the include query ("up to N") and the timeline months that
   hold stacked copies (C9: Immich's timeline decides which photos are stacked copies; v2.5.6's
   search answer carries no `stack`, so the months are read once per check).
2. **exclusions**: each exclusion query read whole, page by page, its ids marked `excluded`
   (design parked note "Keyset": exclusions first, so nothing joins then leaves).
3. **include**: the include query page by page; each photo becomes a pool entry or a mark.
4. **done**: the next step finishes the check (complete, or incomplete when one instant held
   more than a page).

Relative dates resolve in UTC at the check's start (until 2b's timezone).
"""

from __future__ import annotations

import calendar
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field

from central.photo_library.model import (
    CheckMark,
    InPool,
    LeftOut,
    LibraryAsset,
    MediaType,
    PoolEntry,
    SourceQuery,
    TakenBetween,
    TakenLast,
    Waiting,
)
from central.photo_library.ports import KnownRevision
from contracts.models import Model
from media.library_client import ExclusionQuery, KeysetPosition, SearchItem, TakenWindow, TimeBucket


class CheckProgress(Model):
    """Where a check is; stored as the lease's `progress` JSON between steps."""

    phase: Literal["up_to", "exclusions", "include", "done"]
    window: TakenWindow
    exclusion_index: int = Field(default=0, ge=0)
    position: KeysetPosition | None = None
    seen_so_far: int = Field(default=0, ge=0)
    incomplete: bool = False
    stacked_months: tuple[str, ...] = ()   # timeline months whose counts differ (C9)


def window_of(query: SourceQuery, now_utc: float) -> TakenWindow:
    """The query's capture-time bounds for a check starting at `now_utc` (UTC)."""
    match query.taken:
        case TakenBetween(start=start, end=end):
            return TakenWindow(after=start, before=end)
        case TakenLast(years=years, months=months):
            return TakenWindow(after=_months_back(now_utc, 12 * years + months))
    return TakenWindow()


def _months_back(now_utc: float, months: int) -> float:
    now = datetime.fromtimestamp(now_utc, UTC)
    index = now.year * 12 + now.month - 1 - months
    year, month = divmod(index, 12)
    day = min(now.day, calendar.monthrange(year, month + 1)[1])
    return now.replace(year=year, month=month + 1, day=day).timestamp()


def start(query: SourceQuery, now_utc: float) -> CheckProgress:
    return CheckProgress(phase="up_to", window=window_of(query, now_utc))


def exclusions_of(query: SourceQuery) -> tuple[ExclusionQuery, ...]:
    """One Immich query per excluded album, person and tag, and the two screenshot queries."""
    exclude = query.exclude
    queries = [ExclusionQuery(kind="album", ref=ref) for ref in exclude.albums]
    queries += [ExclusionQuery(kind="person", ref=ref) for ref in exclude.people]
    queries += [ExclusionQuery(kind="tag", ref=ref) for ref in exclude.tags]
    if exclude.screenshots:
        queries += [ExclusionQuery(kind="screenshot_name"), ExclusionQuery(kind="screenshot_png")]
    return tuple(queries)


def advance(progress: CheckProgress, query: SourceQuery, *, next_position: KeysetPosition | None,
            seen: int = 0, over_one_instant: bool = False,
            stacked_months: Sequence[str] = ()) -> CheckProgress:
    """The progress after one step: the next page of this phase, or the next phase."""
    seen_so_far = progress.seen_so_far + seen
    match progress.phase:
        case "up_to":
            return progress.model_copy(update={
                "phase": "exclusions" if exclusions_of(query) else "include",
                "stacked_months": tuple(sorted(stacked_months)), "position": None})
        case "exclusions":
            if next_position is not None:
                return progress.model_copy(update={"position": next_position})
            index = progress.exclusion_index + 1
            if index < len(exclusions_of(query)):
                return progress.model_copy(update={"exclusion_index": index, "position": None})
            return progress.model_copy(update={"phase": "include", "position": None})
        case "include":
            if over_one_instant:
                return progress.model_copy(update={
                    "phase": "done", "incomplete": True, "position": None,
                    "seen_so_far": seen_so_far})
            return progress.model_copy(update={
                "phase": "include" if next_position is not None else "done",
                "position": next_position, "seen_so_far": seen_so_far})
    return progress


def month_of(instant: float) -> str:
    moment = datetime.fromtimestamp(instant, UTC)
    return f"{moment.year:04}-{moment.month:02}-01"


def _shift(month: str, by: int) -> str:
    index = int(month[:4]) * 12 + int(month[5:7]) - 1 + by
    return f"{index // 12:04}-{index % 12 + 1:02}-01"


def _around(month: str) -> tuple[str, ...]:
    return tuple(_shift(month, by) for by in (-1, 0, 1))


def months_to_read(items: Sequence[SearchItem],
                   stacked_months: Collection[str]) -> tuple[TimeBucket, ...]:
    """The timeline months to read for a page: each photo taken within a month of a stacked
    month needs the months around its own (its timeline month is local time, within a month of
    its UTC one)."""
    stacked = set(stacked_months)
    needed: set[str] = set()
    for item in items:
        if item.taken_at is None:
            continue
        around = _around(month_of(item.taken_at))
        if stacked.intersection(around):
            needed.update(around)
    return tuple(TimeBucket(month=month) for month in sorted(needed))


def stacked_copies(items: Sequence[SearchItem], stacked_months: Collection[str],
                   shown: Collection[str]) -> frozenset[str]:
    """The page's stacked copies (C9): a photo near a stacked month that the timeline, stacks
    collapsed, does not show in the months around it (`shown`, read for `months_to_read`)."""
    stacked = set(stacked_months)
    return frozenset(
        item.upstream_id for item in items
        if item.taken_at is not None and stacked.intersection(_around(month_of(item.taken_at)))
        and item.upstream_id not in shown)


def needs_fingerprint(items: Sequence[SearchItem], known: Mapping[str, KnownRevision]
                      ) -> tuple[str, ...]:
    """Edited photos whose edit list must be read: no revision is known at their `updatedAt`."""
    return tuple(item.upstream_id for item in items
                 if item.problem is None and item.is_edited and not _known(item, known))


def _known(item: SearchItem, known: Mapping[str, KnownRevision]) -> KnownRevision | None:
    recorded = known.get(item.upstream_id)
    if recorded is not None and item.updated_at is not None \
            and recorded.updated_at == item.updated_at:
        return recorded
    return None


def _media_type(item: SearchItem) -> MediaType:
    if item.kind == "video":
        return "video"
    return "live_photo" if item.live_photo else "photo"


def entries_of(items: Sequence[SearchItem], *, query: SourceQuery, bound_user: str | None,
               connection_ref: str, known: Mapping[str, KnownRevision],
               fingerprints: Mapping[str, str], copies: Collection[str] = frozenset()
               ) -> tuple[tuple[PoolEntry, ...], tuple[CheckMark, ...]]:
    """Each include row's outcome: a mark for a stacked copy; nothing for a partner's photo
    (unless `partners`) or a kind the query did not choose (F-5: a live photo is an image Immich
    pairs with a motion video); else left out with its problem, waiting while Immich has not read
    its size, or in the pool."""
    entries: list[PoolEntry] = []
    marks: list[CheckMark] = []
    for item in items:
        if item.upstream_id in copies or (item.stack_primary is not None
                                           and item.stack_primary != item.upstream_id):
            marks.append(CheckMark(upstream_id=item.upstream_id, mark="stacked_copy"))
            continue
        if (not query.partners and bound_user is not None and item.owner_ref is not None
                and item.owner_ref != bound_user):
            continue
        if item.problem is not None:
            entries.append(LeftOut(upstream_id=item.upstream_id, kind=item.kind,
                                   taken_at=item.taken_at, reason=item.problem))
            continue
        if _media_type(item) not in query.media_types:
            continue
        if (item.width is None or item.height is None
                or (item.kind == "video" and item.duration is None)):
            entries.append(Waiting(upstream_id=item.upstream_id, kind=item.kind,
                                   taken_at=item.taken_at))
            continue
        recorded = _known(item, known)
        if recorded is not None:
            revision = recorded.revision
        elif item.is_edited:
            revision = f"{item.checksum}-{fingerprints[item.upstream_id]}"
        else:
            revision = item.checksum
        entries.append(InPool(asset=LibraryAsset(
            connection_ref=connection_ref, upstream_id=item.upstream_id, revision=revision,
            kind=item.kind, taken_at=item.taken_at, width=item.width, height=item.height,
            duration=item.duration if item.kind == "video" else None),
            updated_at=item.updated_at))
    return tuple(entries), tuple(marks)


def excluded(ids: Sequence[str]) -> tuple[CheckMark, ...]:
    return tuple(CheckMark(upstream_id=ref, mark="excluded") for ref in ids)
