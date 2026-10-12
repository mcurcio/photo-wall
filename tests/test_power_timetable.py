"""The power timetable, pure (Power and the week, slice T, A2; central/power/timetable.py).

Night off 23:00-07:00 every day in Europe/London; holds on top. Every answer is read on the house
clock.
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


def _night_off(frames=None, *, hold_lasts="next-scheduled-change", days=EVERY_DAY) -> NightOff:
    return NightOff(off=ClockAt(time="23:00"), on=ClockAt(time="07:00"), days=days,
                    frames=ChosenFrames(frame_ids=("hall",)) if frames is None else frames,
                    darkness=Darkness.DISPLAY_OFF, hold_lasts=hold_lasts, revision=1)


def _hold(power: Power, started: str, ends: str | None, number: int = 1, frames=("hall", "study")) -> Hold:
    return Hold(hold_id=UUID(int=number), power=power, frame_ids=frames, source=HoldSource(),
                started_at=_at(started), ends_at=None if ends is None else _at(ends))


def _policy(night_off: NightOff, holds=(), *, zone_confirmed=True) -> PowerPolicy:
    return PowerPolicy(night_off, holds=holds, zone_confirmed=zone_confirmed)


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


def test_a_hold_ends_at_the_same_moment_for_a_frame_outside_night_off():
    night_off = _night_off()
    end = hold_end(_policy(night_off), LONDON, _at("2026-10-12T20:00"))
    assert local_date_time(LONDON, end) == "2026-10-12T23:00"
    assert local_date_time(LONDON, hold_end(_policy(night_off), LONDON, _at("2026-10-13T01:00"))) == (
        "2026-10-13T07:00")
    policy = _policy(night_off, holds=(_hold(Power.OFF, "2026-10-12T20:00", "2026-10-12T23:00"),))
    study = frame_timetable("study", policy, LONDON, _at("2026-10-12T20:00"))
    assert (study.now, study.because) == (Power.OFF, "hold")
    assert _words(study) == [("2026-10-12T23:00", "on", "on-underneath")]


def test_until_resumed_has_no_end():
    night_off = _night_off(hold_lasts="until-resumed")
    assert hold_end(_policy(night_off), LONDON, _at("2026-10-12T20:00")) is None
    policy = _policy(night_off, holds=(_hold(Power.ON, "2026-10-12T20:00", None),))
    timetable = frame_timetable("hall", policy, LONDON, _at("2026-10-12T20:00"))
    assert (timetable.now, timetable.because, timetable.changes) == (Power.ON, "hold", ())


def test_no_night_off_within_reach_ends_a_hold_at_the_horizon():
    nobody = _night_off(days=())
    now = _at("2026-10-12T20:00")
    assert hold_end(_policy(nobody), LONDON, now) == horizon_end(LONDON, now)


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
    assert hold_end(policy, LONDON, now) == horizon_end(LONDON, now)
