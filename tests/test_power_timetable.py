"""The power timetable, pure (Power and the week, slice T, A2; central/power/timetable.py).

Night off 23:00-07:00 every day in Europe/London on "hall" ("study" does not follow it); holds on
top. Every answer is read on the house clock.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from central.house_time.calculator import local_date_time
from central.house_time.model import ClockAt, HouseTime
from central.power.model import AllFrames, ChosenFrames, Hold, HoldSource, NightOff, PowerPolicy
from central.power.timetable import (
    HORIZON_DAYS,
    frame_timetable,
    hold_end,
    horizon_end,
    spans,
    unbound_timetable,
)
from contracts.node_output import Darkness, Power

LONDON = HouseTime(zone="Europe/London")
EVERY_DAY = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _at(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo("Europe/London")).timestamp()


def _night_off(frames=None, *, days=EVERY_DAY, on="07:00") -> NightOff:
    return NightOff(off=ClockAt(time="23:00"), on=ClockAt(time=on), days=days,
                    frames=ChosenFrames(frame_ids=("hall",)) if frames is None else frames,
                    darkness=Darkness.DISPLAY_OFF, revision=1)


def _hold(power: Power, started: str, ends: str | None, number: int = 1, frames=("hall", "study")) -> Hold:
    return Hold(hold_id=UUID(int=number), power=power, frame_ids=frames, source=HoldSource(),
                started_at=_at(started), ends_at=None if ends is None else _at(ends))


def _policy(night_off: NightOff, holds=(), *, zone_confirmed=True, hold_lasts="next-scheduled-change") -> PowerPolicy:
    return PowerPolicy(night_off, holds=holds, zone_confirmed=zone_confirmed, hold_lasts=hold_lasts)


def _ends(frame_id: str, policy: PowerPolicy, now: str) -> str:
    return local_date_time(LONDON, hold_end(frame_id, policy, LONDON, _at(now)))


def _words(timetable):
    return [(local_date_time(LONDON, change.at), change.power.value, change.because) for change in timetable.changes]


def test_night_off_spans_and_reasons():
    policy = _policy(_night_off(), holds=())
    found = spans("hall", policy, LONDON, _at("2026-10-12T16:00"), _at("2026-10-13T16:00"))
    assert [(local_date_time(LONDON, s.start), local_date_time(LONDON, s.end), s.power.value, s.because)
            for s in found] == [
        ("2026-10-12T16:00", "2026-10-12T23:00", "on", "on-underneath"),
        ("2026-10-12T23:00", "2026-10-13T07:00", "off", "night-off"),
        ("2026-10-13T07:00", "2026-10-13T16:00", "on", "on-underneath")]
    assert [s.power for s in spans("study", policy, LONDON, _at("2026-10-12T16:00"), _at("2026-10-13T16:00"))] == [
        Power.ON]
    with pytest.raises(ValueError, match="^range$"):
        spans("hall", policy, LONDON, 5.0, 5.0)


def test_a_frames_timetable_now_changes_and_until():
    timetable = frame_timetable("hall", _policy(_night_off(), holds=()), LONDON, _at("2026-10-12T16:00"))
    assert (timetable.now, timetable.because, timetable.darkness) == (Power.ON, "on-underneath", Darkness.DISPLAY_OFF)
    assert _words(timetable)[:2] == [("2026-10-12T23:00", "off", "night-off"),
                                     ("2026-10-13T07:00", "on", "on-underneath")]
    assert local_date_time(LONDON, timetable.until) == "2026-10-20T00:00"
    assert all(change.at == int(change.at) for change in timetable.changes)
    assert all(_at("2026-10-12T16:00") < change.at < timetable.until for change in timetable.changes)
    assert len(timetable.changes) == 2 * HORIZON_DAYS + 1     # 7 nights and the 8th night's off
    assert timetable.wanted_at(_at("2026-10-13T01:00")) == (Power.OFF, "night-off")
    assert timetable.wanted_at(_at("2026-10-12T17:00")) == (Power.ON, "on-underneath")
    assert timetable.next_change_after(_at("2026-10-12T23:00")).power is Power.ON
    during = frame_timetable("hall", _policy(_night_off(), holds=()), LONDON, _at("2026-10-13T03:00"))
    assert (during.now, during.because) == (Power.OFF, "night-off")


def test_until_moves_only_at_house_midnight():
    policy = _policy(_night_off(), holds=())
    morning = frame_timetable("hall", policy, LONDON, _at("2026-10-12T00:00"))
    evening = frame_timetable("hall", policy, LONDON, _at("2026-10-12T23:59"))
    assert morning.until == evening.until == horizon_end(LONDON, _at("2026-10-12T12:00"))
    tomorrow = frame_timetable("hall", policy, LONDON, _at("2026-10-13T00:00"))
    assert tomorrow.until - morning.until == 86400
    # The horizon is whole house days: across the autumn change it is 25 hours longer, never cut.
    assert local_date_time(LONDON, horizon_end(LONDON, _at("2026-10-20T12:00"))) == "2026-10-28T00:00"


def test_a_hold_wins_until_its_end():
    policy = _policy(_night_off(), holds=(_hold(Power.ON, "2026-10-12T23:30", "2026-10-13T07:00"),))
    timetable = frame_timetable("hall", policy, LONDON, _at("2026-10-12T23:45"))
    assert (timetable.now, timetable.because) == (Power.ON, "hold")
    first = timetable.changes[0]
    assert (local_date_time(LONDON, first.at), first.power) == ("2026-10-13T23:00", Power.OFF)
    held = spans("hall", policy, LONDON, _at("2026-10-12T23:45"), _at("2026-10-13T08:00"))[0]
    assert (held.because, held.hold_id, held.source) == ("hold", UUID(int=1), HoldSource())


def test_a_hold_on_a_frame_in_night_off_ends_at_its_next_off_or_on():
    """Owner, 2026-10-12: "Each Frame's hold ends at its own next change".

    Mutation probe (P-on-time-for-all): end every Frame's hold at Night off's on time -> the hall,
    held at 20:00, stays held through 23:00 to 07:00."""
    night_off = _night_off()
    assert _ends("hall", _policy(night_off), "2026-10-12T20:00") == "2026-10-12T23:00"
    assert _ends("hall", _policy(night_off), "2026-10-13T01:00") == "2026-10-13T07:00"     # in the night
    # Night off on Fridays only: Monday's hold lasts to Friday's 23:00, its own next change.
    assert _ends("hall", _policy(_night_off(days=("fri",))), "2026-10-12T20:00") == "2026-10-16T23:00"


def test_a_hold_on_a_frame_outside_night_off_ends_at_the_next_on_time():
    """Owner, 2026-10-12: "a Frame with no Night off holds until 07:00 tomorrow": the next time of
    day Night off turns displays on, every day, and it follows Night off's on time when that
    changes. The hall (in Night off) is held to 23:00 the same evening; the study to 07:00.

    Mutation probe (P-house-wide-end): end every Frame's hold at Night off's next on or off time
    (the old house-wide rule) -> the study's hold ends at 23:00."""
    night_off = _night_off()
    assert _ends("study", _policy(night_off), "2026-10-12T20:00") == "2026-10-13T07:00"
    assert _ends("study", _policy(night_off), "2026-10-12T06:00") == "2026-10-12T07:00"
    assert _ends("study", _policy(_night_off(on="08:30")), "2026-10-12T20:00") == "2026-10-13T08:30"
    assert _ends("study", _policy(_night_off(days=("fri",))), "2026-10-12T20:00") == "2026-10-13T07:00"
    hall, study = (hold_end(frame_id, _policy(night_off), LONDON, _at("2026-10-12T20:00")) for frame_id in ("hall", "study"))
    policy = _policy(night_off, holds=(_hold(Power.OFF, "2026-10-12T20:00", "2026-10-12T23:00", frames=("hall",)),
                                       _hold(Power.OFF, "2026-10-12T20:00", "2026-10-13T07:00", frames=("study",))))
    assert (hall, study) == (_at("2026-10-12T23:00"), _at("2026-10-13T07:00"))
    timetable = frame_timetable("study", policy, LONDON, _at("2026-10-12T20:00"))
    assert (timetable.now, timetable.because) == (Power.OFF, "hold")
    assert _words(timetable) == [("2026-10-13T07:00", "on", "on-underneath")]


def test_a_hold_pressed_after_midnight_ends_the_same_morning():
    """Pressed at 01:00, "07:00 tomorrow" is this morning's 07:00: the next one, not the next day's.

    Mutation probe (P-next-day): look for the on time from the house day after now's -> the
    study's 01:00 hold lasts to the next day's 07:00."""
    night_off = _night_off()
    assert _ends("study", _policy(night_off), "2026-10-13T01:00") == "2026-10-13T07:00"
    assert _ends("hall", _policy(night_off), "2026-10-13T01:00") == "2026-10-13T07:00"


def test_a_hold_across_the_autumn_daylight_saving_night_ends_at_07_00_house_time():
    """On 25 Oct 2026 London's clocks go back at 02:00 BST: a hold pressed at 23:30 BST the night
    before ends at 07:00 GMT, 8.5 hours later, on both Frames.

    Mutation probe (P-fixed-offset): work out the on time as the house day's start plus seven hours
    of seconds -> 06:00 GMT."""
    night_off = _night_off()
    for frame_id in ("hall", "study"):
        end = hold_end(frame_id, _policy(night_off), LONDON, _at("2026-10-24T23:30"))
        assert local_date_time(LONDON, end) == "2026-10-25T07:00"
        assert end - _at("2026-10-24T23:30") == 8.5 * 3600


def test_until_resumed_has_no_end():
    night_off = _night_off()
    for frame_id in ("hall", "study"):
        assert hold_end(frame_id, _policy(night_off, hold_lasts="until-resumed"), LONDON, _at("2026-10-12T20:00")) is None
    policy = _policy(night_off, holds=(_hold(Power.ON, "2026-10-12T20:00", None),), hold_lasts="until-resumed")
    timetable = frame_timetable("hall", policy, LONDON, _at("2026-10-12T20:00"))
    assert (timetable.now, timetable.because, timetable.changes) == (Power.ON, "hold", ())


def test_no_night_off_within_reach_ends_a_hold_at_the_horizon():
    nobody = _night_off(days=())
    now = _at("2026-10-12T20:00")
    assert hold_end("hall", _policy(nobody), LONDON, now) == horizon_end(LONDON, now)


def test_the_newest_hold_wins():
    older = _hold(Power.OFF, "2026-10-12T18:00", None, number=1)
    newer = _hold(Power.ON, "2026-10-12T19:00", "2026-10-12T21:00", number=2)
    timetable = frame_timetable("study", _policy(_night_off(), holds=(newer, older)), LONDON,
                                _at("2026-10-12T19:30"))
    assert (timetable.now, timetable.because) == (Power.ON, "hold")
    assert _words(timetable) == [("2026-10-12T21:00", "off", "hold")]


def test_all_frames_follow_night_off_and_an_unbound_output_is_on():
    policy = _policy(_night_off(AllFrames()), holds=())
    assert _words(frame_timetable("anything", policy, LONDON, _at("2026-10-12T16:00")))[0][1] == "off"
    unbound = unbound_timetable(LONDON, _at("2026-10-12T16:00"))
    assert (unbound.now, unbound.changes, unbound.darkness) == (Power.ON, (), Darkness.DISPLAY_OFF)
    assert unbound.until == horizon_end(LONDON, _at("2026-10-12T16:00"))


def test_night_off_switches_nothing_until_the_houses_zone_is_confirmed():
    """Central's own zone need not be the house's (a container in UTC puts 23:00 at 16:00 in Los
    Angeles): until Settings › House is saved, Night off makes no span and a hold lasts to the
    horizon.

    Mutation probe: ignore `zone_confirmed` -> the hall is off at 23:00."""
    policy = _policy(_night_off(), zone_confirmed=False)
    now = _at("2026-10-12T16:00")
    assert [span.because for span in spans("hall", policy, LONDON, now, _at("2026-10-13T16:00"))] == ["on-underneath"]
    assert frame_timetable("hall", policy, LONDON, now).changes == ()
    assert hold_end("hall", policy, LONDON, now) == hold_end("study", policy, LONDON, now) == horizon_end(LONDON, now)
