"""Pause and Carry on through the real Coordinator over PostgreSQL (decision 0036, slice 3; the
owner's rulings S3, S4, S6 and S8).

A Frame holds the photo it shows: Central adds a hold above every Run from the shared instant to
the pause's end and never changes an offered layer; the schedule beneath keeps advancing, and Carry
on or the pause's end reveals its current state. Nothing here claims what a screen showed: the
Player's drawing of a hold and of a cut is the executor's tests and the bench's.
"""
from __future__ import annotations

import itertools
import logging
from uuid import uuid4

import pytest
from support.photo_pools import photo
from test_coordination import report
from test_skip_never_show import PHOTOS, Wall

from central.db import DatabaseTransactionClock
from central.everyday.model import END_LEAD, PAUSE_SECONDS, PauseRefusal
from central.everyday.policy import photo_of, showing_at
from central.frame_status.model import Action, OverrideKind
from contracts.node_output import Power
from central.infra.power_store import PgPower
from central.registry import RegistryError
from central.runtime import PAUSE_RANK, Contribution, Scene

pytestmark = pytest.mark.db


def pause(wall, *frames, request_id="pause-1", seconds=PAUSE_SECONDS):
    return wall.coordinator.pause(request_id, frames, seconds, by="person:matt", facts=wall.status.everyday_in)


def hold_of(wall, frame_id):
    return next(layer for layer in wall.layers(frame_id) if layer.action and layer.priority == PAUSE_RANK)


def test_the_photo_stays_to_the_pauses_end(registry):
    """The hold shows the photo the Frame showed at the shared instant until the pause's end, above
    the Frame's own turns, which go on beneath it unchanged. Mutation probe: leave the holds out of
    the planning pass."""
    wall = Wall(registry, {"hall": "wall"})
    shown = photo_of(wall.showing("hall"))
    before = {layer.assignment_id: layer for layer in wall.layers("hall")}
    answer = pause(wall, "hall")
    assert [each.frame_id for each in answer.paused] == ["hall"] and answer.stayed == ()
    wall.tick()
    hold = hold_of(wall, "hall")
    assert (photo_of(hold), hold.start, hold.end) == (shown, answer.ends_at - PAUSE_SECONDS, answer.ends_at)
    for offset in (5, 200, 3000):
        wall.tick(offset - (registry.clock.utc() - hold.start))
        assert showing_at(wall.layers("hall"), registry.clock.utc()) == hold
    after = {layer.assignment_id: layer for layer in wall.layers("hall")}
    assert all(after[key] == layer for key, layer in before.items() if key in after)
    assert any(not layer.action for layer in wall.layers("hall"))     # the schedule goes on beneath


def test_no_placement_gives_the_held_photo_to_another_frame(registry):
    """While a hold lasts no new placement gives its photo to another Frame (its extent counts
    against every placement); a pause is never refused for a photo due elsewhere (S4)."""
    wall = Wall(registry, {"hall": "wall", "desk": "wall"}, cycle=30, photos=PHOTOS[:4])
    held = photo_of(wall.showing("hall"))
    answer = pause(wall, "hall", seconds=900)
    offered = {layer.assignment_id for layer in wall.layers("desk")}
    for _ in range(20):
        wall.tick(30)
        for layer in wall.layers("desk"):
            if layer.assignment_id not in offered and layer.start < answer.ends_at:
                assert photo_of(layer) != held, layer
            offered.add(layer.assignment_id)


def test_pause_is_refused_on_a_video(registry):
    """S8: no Pause on a video, refused `video` (and absent from `can`)."""
    film = photo("film", taken_at=900, width=2400, height=3600, kind="video")
    wall = Wall(registry, {"hall": "wall"}, photos=(film,))
    assert wall.showing("hall").variant.media_type == "video/mp4"
    assert Action.PAUSE not in wall.frame_status("hall").can
    with pytest.raises(RegistryError) as refused:
        pause(wall, "hall")
    assert refused.value.code == "nothing-paused"
    assert refused.value.details["stayed"] == [{"frame_id": "hall", "reason": PauseRefusal.VIDEO.value}]


def test_carry_on_reveals_the_schedules_current_state_at_once(registry):
    """S3: Carry on ends the pause at once: its hold is cut `END_LEAD` and its fade after the press,
    and what shows from the cut is the schedule's current turn (it kept advancing beneath), not
    the turn the pause began on. Mutation probe: end the hold at its own end on Carry on."""
    wall = Wall(registry, {"hall": "wall"}, cycle=60)
    begun = wall.showing("hall")
    answer = pause(wall, "hall")
    wall.tick()
    wall.tick(400)
    ended = wall.coordinator.carry_on(answer.pause_id, by="person:matt")
    now = registry.clock.utc()
    assert [each.frame_id for each in ended.ended] == ["hall"]
    wall.tick()
    hold = hold_of(wall, "hall")
    assert hold.cut_at == ended.until and ended.until <= now + END_LEAD + hold.fade_out + 0.001
    revealed = showing_at(wall.layers("hall"), ended.until)
    assert revealed is not None and not revealed.action
    assert revealed.assignment_id != begun.assignment_id and revealed.start > begun.start


def test_the_pauses_end_reveals_the_schedules_current_state(registry):
    wall = Wall(registry, {"hall": "wall"}, cycle=60)
    begun = wall.showing("hall")
    answer = pause(wall, "hall", seconds=300)
    wall.tick()
    hold = hold_of(wall, "hall")
    assert hold.end == answer.ends_at and hold.cut_at is None
    wall.tick(answer.ends_at - registry.clock.utc() - 30)
    revealed = showing_at(wall.layers("hall"), answer.ends_at)
    assert revealed is not None and not revealed.action and revealed.start > begun.start
    wall.tick(60)
    assert all(layer.assignment_id != hold.assignment_id for layer in wall.layers("hall"))
    assert all(each.kind is not OverrideKind.PAUSE for each in wall.status.home().overrides)


def test_night_off_wins_and_the_pause_still_ends_at_its_time(registry):
    """Power wins: a hold to off during a pause turns the display off (the Frame rests), and the
    pause still ends at its own time: its hold is not moved."""
    wall = Wall(registry, {"hall": "wall"})
    answer = pause(wall, "hall")
    wall.tick()
    hold = hold_of(wall, "hall")
    PgPower(registry.db, times=DatabaseTransactionClock()).all_off(uuid4(), by="person:matt")
    wall.tick(5)
    frame = wall.frame_status("hall")
    assert frame.override.kind is OverrideKind.POWER_HOLD and frame.override.power is Power.OFF
    assert Action.PAUSE not in frame.can and Action.SKIP not in frame.can      # it rests
    paused = next(each for each in wall.status.home().overrides if each.kind is OverrideKind.PAUSE)
    assert paused.until == answer.ends_at
    assert hold_of(wall, "hall") == hold and hold.end == answer.ends_at


def test_a_paused_frame_stays_paused_when_show_now_starts(registry):
    """S6: a Show now admitted during a pause plays beneath the hold, which stays on top, and
    appears at Carry on."""
    wall = Wall(registry, {"hall": "wall"})
    answer = pause(wall, "hall")
    wall.tick()
    hold = hold_of(wall, "hall")
    wall.coordinator.runtime.command("set_scene", Scene(scene_id="holidays", cycle_seconds=600, contributions=(
        Contribution(target="frame:hall", source_refs=("library:1",)),)), by="console")
    wall.coordinator.runtime.command("activate", "holidays", "show-now", registry.clock.utc(), by="console",
                                     priority=3)
    wall.tick(5)
    assert showing_at(wall.layers("hall"), registry.clock.utc() + 10) == hold
    ended = wall.coordinator.carry_on(answer.pause_id, "hall", by="person:matt")
    wall.tick()
    revealed = showing_at(wall.layers("hall"), ended.until)
    assert revealed is not None and revealed.priority == 3


def test_a_double_tap_is_one_pause(registry):
    wall = Wall(registry, {"hall": "wall"})
    first = pause(wall, "hall")
    registry.clock.advance(1)
    again = pause(wall, "hall")
    assert again.duplicate and again.ends_at == first.ends_at and again.paused == first.paused
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM frame_pauses").fetchone()["n"] == 1
    with pytest.raises(RegistryError) as refused:
        pause(wall, "hall", request_id="pause-2")
    assert refused.value.details["stayed"] == [{"frame_id": "hall", "reason": "paused"}]


def test_a_pause_never_starves_its_players_offers(registry, caplog):
    """0036 build step 0's check with an hour's hold: a Frame paused for an hour on 10 s turns is
    offered every pass whose proposal changes for 40 minutes, and the turns beneath keep coming
    (prepared for the reveal)."""
    wall = Wall(registry, {"hall": "wall"}, cycle=10)
    pause(wall, "hall")
    wall.tick()
    caplog.set_level(logging.INFO, logger="central.coordination")
    player = wall.players["hall"]
    sequence = itertools.count(100)
    end = registry.clock.utc() + 40 * 60
    starved = []
    while registry.clock.utc() < end:
        caplog.clear()
        wall.tick(10)
        if any("offer_backpressure" in record.getMessage() for record in caplog.records):
            starved.append(registry.clock.utc())
        wall.coordinator.readiness(player["player_id"], report(wall.coordinator, player, sequence=next(sequence)))
        now = registry.clock.utc()
        assert any(not layer.action and layer.start <= now < layer.end for layer in wall.layers("hall")), now
        assert showing_at(wall.layers("hall"), now).priority == PAUSE_RANK
    assert starved == []


def test_a_paused_frame_carries_its_pause_and_reads_requested_until_its_hold_is_presented(registry):
    """Home's reading of a paused Frame: its override is the pause by the person until its end,
    Skip and Pause are not offered on it, and the pause reads requested until the Player reports
    its hold presented, then done. Mutation probe: leave the pauses out of `overrides`."""
    wall = Wall(registry, {"hall": "wall"})
    answer = pause(wall, "hall")
    wall.tick()
    frame = wall.frame_status("hall")
    assert (frame.asked.action, frame.asked.state) == ("pause", "requested")
    assert (frame.override.kind, frame.override.by, frame.override.until) == (
        OverrideKind.PAUSE, "person:matt", answer.ends_at)
    assert Action.SKIP not in frame.can and Action.PAUSE not in frame.can
    wall.tick(3)
    wall.presented("hall", hold_of(wall, "hall"))
    assert wall.frame_status("hall").asked.state == "done"
