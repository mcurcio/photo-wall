"""Each Scene's line of photos across passes (roadmap 5a, run ledger .claude/runs/photo-5a.md, slice
O1): central/wall_plan/order.py through its public functions. Pure; no database."""

from datetime import UTC, date, datetime

import pytest

from central.catalog import Candidate
from central.wall_plan.order import (
    PhotoShowing,
    SceneLine,
    ScenePass,
    WallHistory,
    last_shown_at,
    pass_sequence,
    scene_passes,
)
from central.wall_plan.selection import SceneSelection

DAY = 86400.0
NOW = datetime(2026, 10, 11, 12, tzinfo=UTC).timestamp()
TODAY = date(2026, 10, 11)
SHUFFLE = SceneSelection()


def candidate(asset_id: str, captured_at: float = 0.0) -> Candidate:
    return Candidate(asset_id=asset_id, kind="image", original_width=4000, original_height=3000,
                     captured_at=captured_at, readable_size=(4000, 3000))


def pool(count: int) -> tuple[Candidate, ...]:
    return tuple(candidate(f"photo-{i:02d}", captured_at=1000.0 + i) for i in range(count))


def showing(asset_id: str, *, scene: str = "scene", pass_number: int = 1, start: float = NOW - 60,
            end: float | None = None, frame: str = "frame-1", after_end: str = "leave_as_is",
            assignment: str | None = None) -> PhotoShowing:
    return PhotoShowing(
        assignment_id=assignment or f"a-{scene}-{asset_id}-{pass_number}-{int(start)}",
        asset_id=asset_id, frame_id=frame, scene_id=scene, pass_number=pass_number, start=start,
        end=start + 30 if end is None else end, fade_in=0, after_end=after_end)


def line(candidates, selection=SHUFFLE, *, scene_pass=None, shown_at=None, pickable=None,
         today=TODAY):
    return SceneLine("scene", selection, candidates, scene_pass or ScenePass(scene_id="scene"),
                     shown_at or {}, now=NOW, today=today,
                     pickable=frozenset(c.asset_id for c in candidates)
                     if pickable is None else frozenset(pickable))


def sequence(candidates, selection=SHUFFLE, *, pass_number=1, after=None, today=TODAY):
    return pass_sequence(candidates, selection, scene_id="scene", pass_number=pass_number,
                         after=after, today=today)


# 1. Shuffle.

def test_a_shuffled_pass_holds_every_photo_once_and_a_new_pass_reshuffles():
    photos = pool(20)
    first = sequence(photos)
    assert sorted(first) == sorted(c.asset_id for c in photos)
    assert sequence(photos, pass_number=2) != first
    # Deterministic: the same pass is the same order, whatever order the pool arrives in.
    assert sequence(tuple(reversed(photos))) == first


def test_adding_a_photo_keeps_the_others_relative_order():
    photos = pool(20)
    before = sequence(photos)
    after = sequence((*photos, candidate("photo-new", captured_at=5000)))
    assert tuple(a for a in after if a != "photo-new") == before
    assert sequence(photos[1:]) == tuple(a for a in before if a != photos[0].asset_id)


# 2. Rolling a pass.

def test_the_pass_rolls_exactly_when_the_last_pickable_photo_is_picked():
    photos = pool(3)
    scene_line = line(photos)
    order = scene_line.ranked()
    assert [scene_line.pick(order[0]), scene_line.pick(order[1])] == [1, 1]
    assert scene_line.pass_number == 1
    assert scene_line.pick(order[2]) == 1
    assert scene_line.pass_number == 2
    # The new pass starts with nothing picked: every photo is back in the first tier.
    assert sorted(scene_line.ranked()) == sorted(order)
    assert scene_line.pick(scene_line.ranked()[0]) == 2


def test_an_unpickable_photo_never_holds_a_pass_open():
    photos = pool(3)
    scene_line = line(photos, pickable={"photo-00", "photo-01"})
    scene_line.pick("photo-00")
    assert scene_line.pass_number == 1
    scene_line.pick("photo-01")
    assert scene_line.pass_number == 2


def test_a_repeat_in_the_pass_changes_nothing_and_an_unknown_photo_is_refused():
    scene_line = line(pool(2))
    assert scene_line.pick("photo-00") == 1
    assert scene_line.pick("photo-00") == 1
    assert scene_line.pass_number == 1
    with pytest.raises(KeyError):
        scene_line.pick("not-in-the-line")
    with pytest.raises(ValueError):
        line(pool(2), pickable={"not-in-the-line"})


def test_a_pass_the_history_completed_is_over_before_the_next_pick():
    photos = pool(2)
    scene_line = line(photos, scene_pass=ScenePass(
        scene_id="scene", pass_number=4, picked=frozenset({"photo-00", "photo-01"})))
    assert scene_line.pass_number == 5
    assert scene_line.pick(scene_line.ranked()[0]) == 5


# 3. Tiers.

def test_a_photo_shown_within_the_no_repeat_days_ranks_after_every_unshown_photo():
    photos = pool(10)
    recent = sequence(photos)[0]
    week = SceneSelection(no_repeat_days=7)
    assert line(photos, week, shown_at={recent: NOW - 2 * DAY}).ranked()[-1] == recent
    # Outside the window it is an ordinary photo again.
    assert line(photos, week, shown_at={recent: NOW - 8 * DAY}).ranked()[0] == recent
    # No-repeat off: shown or not, it keeps its place.
    off = SceneSelection(no_repeat_days=0)
    assert line(photos, off, shown_at={recent: NOW - 2 * DAY}).ranked()[0] == recent


def test_a_photo_picked_in_this_pass_ranks_last_even_after_recently_shown_ones():
    photos = pool(10)
    order = sequence(photos)
    picked, recent = order[0], order[1]
    scene_line = line(photos, SceneSelection(no_repeat_days=7),
                      scene_pass=ScenePass(scene_id="scene", picked=frozenset({picked})),
                      shown_at={recent: NOW - DAY})
    ranked = scene_line.ranked()
    assert ranked[-2:] == (recent, picked)


# 4. A repeat rather than nothing.

def test_a_line_whose_photos_were_all_shown_yesterday_still_ranks_every_one():
    photos = pool(4)
    shown_at = {c.asset_id: NOW - DAY for c in photos}
    ranked = line(photos, SceneSelection(no_repeat_days=7), shown_at=shown_at).ranked()
    assert ranked == sequence(photos)


# 5. Chronological, newest first, On this day.

def test_chronological_continues_after_the_last_shown_photo_and_wraps():
    photos = (candidate("c", 300), candidate("a", 100), candidate("d", 400), candidate("b", 200))
    chronological = SceneSelection(order="chronological")
    assert sequence(photos, chronological) == ("a", "b", "c", "d")
    assert sequence(photos, chronological, after=photos[0]) == ("d", "a", "b", "c")
    # Through the line: the Scene's last shown photo sets where it continues.
    scene_line = line(photos, chronological,
                      scene_pass=ScenePass(scene_id="scene", last_shown="b"))
    assert scene_line.ranked() == ("c", "d", "a", "b")


def test_newest_first_is_descending():
    photos = (candidate("old", 100), candidate("new", 300), candidate("mid", 200))
    assert sequence(photos, SceneSelection(order="newest_first")) == ("new", "mid", "old")


def test_on_this_day_puts_todays_date_in_earlier_years_first_newest_year_first():
    def taken(year, month, day):
        return datetime(year, month, day, 9, tzinfo=UTC).timestamp()

    photos = (
        candidate("2019-10-11", taken(2019, 10, 11)),
        candidate("2023-10-11", taken(2023, 10, 11)),
        candidate("2023-10-12", taken(2023, 10, 12)),
        candidate("2021-03-01", taken(2021, 3, 1)),
        candidate("2016-10-11", taken(2016, 10, 11)),
    )
    on_this_day = sequence(photos, SceneSelection(order="on_this_day"))
    assert on_this_day[:3] == ("2023-10-11", "2019-10-11", "2016-10-11")
    rest = (photos[2], photos[3])
    assert on_this_day[3:] == sequence(rest)


# 6. A Scene's pass from the history.

def test_scene_passes_take_the_highest_pass_across_shown_rows_and_live_selections():
    history = WallHistory(
        shown=(showing("p1", pass_number=1, start=NOW - 300),
               showing("p2", pass_number=2, start=NOW - 200),
               showing("p3", pass_number=2, start=NOW - 100),
               showing("q1", scene="other", pass_number=1, start=NOW - 50)),
        selected=(showing("p4", pass_number=3, start=NOW + 10),
                  showing("q2", scene="other", pass_number=1, start=NOW + 10)),
    )
    passes = scene_passes(history)
    assert passes["scene"] == ScenePass(scene_id="scene", pass_number=3,
                                        picked=frozenset({"p4"}), last_shown="p3")
    assert passes["other"] == ScenePass(scene_id="other", pass_number=1,
                                        picked=frozenset({"q1", "q2"}), last_shown="q1")
    assert scene_passes(WallHistory()) == {}


def test_last_shown_at_is_each_photos_latest_end_on_any_frame_and_scene():
    history = WallHistory(shown=(
        showing("p", start=NOW - 500, end=NOW - 400, frame="frame-1"),
        showing("p", scene="other", start=NOW - 100, end=NOW - 50, frame="frame-2"),
        showing("q", start=NOW - 300, end=NOW - 200),
    ), selected=(showing("r", start=NOW, end=NOW + 30),))
    assert last_shown_at(history) == {"p": NOW - 50, "q": NOW - 200}
