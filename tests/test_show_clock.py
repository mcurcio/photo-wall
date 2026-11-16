"""The Player follows Central's time on its own monotonic clock (player/show_clock.py).

Through the real Executor and PlayerService: the 2026-11-08 first boot (a Pi's wall clock 975,402 s
behind Central's, every plan refused, the Frame black), and what must hold around it: a wall-clock
step changes nothing, Central's own step re-anchors, jitter never redraws a still, a session gap
keeps the picture, and no silence, however long, ends a grant (decision 0032: the bound is a
quality, never a gate).
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import replace

import pytest
from test_executor import Rig, layer
from test_player_service import Server, close, pending, rig

from contracts.models import Commit
from contracts.time import ManualClock
from player.show_clock import DRIFT_RATE, ShowClock

BEHIND = 975_402.0


def clock_at(central: float = 100.0, monotonic: float = 0.0) -> tuple[ManualClock, ShowClock]:
    """A monotonic clock and a ShowClock anchored exactly on Central's `central`."""
    clock = ManualClock(central, monotonic)
    show = ShowClock(clock.monotonic)
    assert show.sample(server_time=central, sent_monotonic=monotonic,
                       received_monotonic=monotonic) == "anchored"
    return clock, show


def shown(service) -> list[str]:
    return [local.layer.assignment_id for local in service.renderer.outputs["hdmi1"].layers]


# -- The clock's rules ------------------------------------------------------------------------------

def test_before_the_first_sample_it_knows_nothing_and_claims_nothing():
    clock = ManualClock(100)
    show = ShowClock(clock.monotonic)
    assert not show.anchored
    assert show.now() == -math.inf and show.timebase() == -math.inf
    assert show.uncertainty == math.inf
    assert show.diagnostics.status == "no_sample"
    assert show.diagnostics.uncertainty is None     # JSON-safe


@pytest.mark.parametrize("drift_rate", [0, -1, math.inf, math.nan])
def test_the_drift_rate_must_be_finite_and_positive(drift_rate):
    with pytest.raises(ValueError):
        ShowClock(lambda: 0.0, drift_rate=drift_rate)


def test_the_first_sample_anchors_at_the_midpoint():
    clock = ManualClock(100, 1.02)
    show = ShowClock(clock.monotonic)
    assert show.sample(server_time=200, sent_monotonic=1, received_monotonic=1.02) == "anchored"
    assert show.timebase() == pytest.approx(200.01 - 1.02)
    assert show.uncertainty == pytest.approx(.01)


@pytest.mark.parametrize("sample", [
    dict(server_time=math.nan, sent_monotonic=0, received_monotonic=0),
    dict(server_time=100, sent_monotonic=math.inf, received_monotonic=0),
    dict(server_time=100, sent_monotonic=0, received_monotonic=math.nan),
    dict(server_time=100, sent_monotonic=1, received_monotonic=0),
])
def test_an_impossible_reading_is_counted_and_changes_nothing(sample):
    clock, show = clock_at()
    before = show.timebase(), show.uncertainty
    assert show.sample(**sample) == "impossible"
    assert (show.timebase(), show.uncertainty) == before
    assert show.diagnostics.rejected == 1 and show.diagnostics.accepted == 1


def test_a_six_second_answer_anchors_an_unanchored_clock():
    """Any answer anchors, however slow (the 2026-11-15 freeze: 3-7 s answers were thrown away
    and every photo refused). Mutation: restore the `rtt / 2 > threshold` rejection -> refused."""
    clock = ManualClock(100, 6)
    show = ShowClock(clock.monotonic)
    assert show.sample(server_time=200, sent_monotonic=0, received_monotonic=6) == "anchored"
    assert show.anchored and show.uncertainty == pytest.approx(3.0)
    assert math.isfinite(show.now()) and show.now() == pytest.approx(203)
    assert show.diagnostics.status == "anchored" and show.diagnostics.uncertainty == pytest.approx(3.0)


def test_a_slow_consistent_answer_never_loosens_a_good_anchor():
    """Anchored at 0.005 s, a 6 s answer whose interval holds the estimate is kept and the bound
    stays. Mutation: replace `min(u, far)` by `far` -> the bound becomes about 6 s."""
    clock, show = clock_at()
    show._anchor = replace(show._anchor, bound=.005)
    clock.advance(10)
    timebase = show.timebase()
    # Central read at 105, so at receipt (110) its time is in [105, 111]; the estimate is 110.
    assert show.sample(server_time=105, sent_monotonic=4, received_monotonic=10) == "kept"
    assert show.timebase() == timebase
    assert show.uncertainty == pytest.approx(.005 + DRIFT_RATE * 10)


def test_an_answer_disjoint_from_the_anchor_re_anchors_at_its_midpoint():
    """Central's clock moved (or the old anchor was wrong): the answer's own interval wins.
    Mutation: never re-anchor on a disjoint answer -> the timebase stands."""
    clock, show = clock_at()
    clock.advance(10)
    # The estimate is 110 +- 0.001; Central says [150, 151] at receipt.
    assert show.sample(server_time=150, sent_monotonic=9, received_monotonic=10) == "anchored"
    assert show.now() == pytest.approx(150.5) and show.uncertainty == pytest.approx(.5)


def test_an_overlapping_answer_narrows_the_anchor_to_the_intersection():
    """The estimate's interval [e - u, e + u] meets the answer's interval but the estimate lies
    outside it: anchor at the intersection's midpoint, bound half its width. Mutation: treat
    "outside I" as disjoint -> the anchor jumps to I's midpoint with bound rtt / 2."""
    clock, show = clock_at()
    show._anchor = replace(show._anchor, bound=1.0)
    clock.advance(10)
    # e = 110, u = 1.001: [108.999, 111.001]; I = [110.5, 112.5]; J = [110.5, 111.001].
    assert show.sample(server_time=110.5, sent_monotonic=8, received_monotonic=10) == "anchored"
    u = 1.0 + DRIFT_RATE * 10
    assert show.now() == pytest.approx((110.5 + 110 + u) / 2)
    assert show.uncertainty == pytest.approx((110 + u - 110.5) / 2)


def test_a_kept_sample_reports_its_far_edge_never_less_than_the_true_bound():
    """The estimate is inside the interval but off its midpoint; the true error can be as much as
    the far edge, so the far edge is what is reported. Mutation: report rtt / 2 -> this fails."""
    clock, show = clock_at()
    show._anchor = replace(show._anchor, bound=1.0)
    clock.advance(10.05)
    # Central read at 110.01, so at receipt its time is in [110.01, 110.06]; the estimate is 110.05.
    assert show.sample(server_time=110.01, sent_monotonic=10, received_monotonic=10.05) == "kept"
    assert show.uncertainty == pytest.approx(.04)
    assert show.timebase() == 100


def test_uncertainty_grows_with_the_samples_age():
    clock, show = clock_at()
    clock.advance(500)
    assert show.uncertainty == pytest.approx(DRIFT_RATE * 500)
    assert show.diagnostics.sample_age == pytest.approx(500)


# -- Behaviour through the real Executor and PlayerService ---------------------------------------

def test_a_pi_clock_975402_seconds_behind_central_shows_the_photo_at_its_planned_instant(tmp_path):
    """The 2026-11-08 first boot replayed: the plan arrives before any sample, and the Pi's wall
    clock is 11 days behind. Mutation: restore the issued_at check -> the plan is refused."""
    async def check():
        central, pi = ManualClock(100), ManualClock(100 - BEHIND)
        service, server = await rig(tmp_path, server=Server(central), clock=pi, probe=False)
        try:
            picture = layer(start=102, media_origin=102, data=server.data)
            server.offer(revision=2, layers=(picture,))
            await service.poll_state()
            assert await service.probe_time()
            service._feedback()
            assert await service.download(service._jobs[0])
            service.tick_main()
            readiness = service._feedback()
            assert readiness.prepared == ("picture",) and readiness.failures == ()
            assert readiness.observed_at == pytest.approx(100)
            commit = Commit(plan_id="offered", revision=2, authority_epoch=1,
                            readiness_sequence=readiness.sequence, assignment_ids=("picture",),
                            committed_at=central.utc())
            server.offer(revision=2, layers=(picture,), commits=(commit,))
            await service.poll_state()
            service.tick_main()
            assert shown(service) == []             # before its instant
            central.advance(2), pi.advance(2)
            assert await service.probe_time()
            service.tick_main()
            assert shown(service) == ["picture"]    # at its instant
            assert [(o.status, o.observed_at) for o in pending(service)] == [("presented", pytest.approx(102))]
        finally:
            await close(service)
    asyncio.run(check())


@pytest.mark.parametrize("step", [BEHIND, -3600.0])
def test_a_wall_clock_step_while_a_photo_plays_changes_nothing(tmp_path, step):
    async def check():
        central, pi = ManualClock(100), ManualClock(100)
        service, server = await rig(tmp_path, server=Server(central), clock=pi)
        try:
            service._feedback()
            assert await service.download(service._jobs[0])
            service.tick_main()
            readiness = service._feedback()
            commit = Commit(plan_id="offered", revision=1, authority_epoch=1,
                            readiness_sequence=readiness.sequence, assignment_ids=("picture",),
                            committed_at=central.utc())
            server.offer(commits=(commit,))
            await service.poll_state()
            service.tick_main()
            drawn = service.renderer.outputs["hdmi1"]
            assert shown(service) == ["picture"]

            pi.step_utc(step)
            assert await service.probe_time()
            service.tick_main()

            after = service.renderer.outputs["hdmi1"]
            assert shown(service) == ["picture"] and after.timebase == drawn.timebase
            readiness = service._feedback()
            assert readiness.prepared == ("picture",)
            assert readiness.failures == () and readiness.lost_grants == ()
        finally:
            await close(service)
    asyncio.run(check())


@pytest.mark.parametrize("field,value", [("time_epoch", 2), ("time_player", "another-player")])
def test_a_probe_naming_another_epoch_or_player_leaves_anchor_and_picture_untouched(tmp_path, field, value):
    """Mutation probe: drop the player check in `probe_time`."""
    async def check():
        service, server = await rig(tmp_path)
        try:
            before = service.show_clock.timebase(), service.show_clock.diagnostics.samples
            setattr(server, field, value)
            server.time_offset = 50     # were it believed, the timeline would move
            assert not await service.probe_time()
            assert (service.show_clock.timebase(), service.show_clock.diagnostics.samples) == before
        finally:
            await close(service)
    asyncio.run(check())


def test_before_the_first_sample_nothing_is_due_or_expired_and_downloads_proceed(tmp_path):
    """No time from Central yet: the plan stands, its bytes download, nothing is drawn and no
    Readiness is posted. Mutation: gate the download jobs on the anchor -> this fails."""
    async def check():
        service, server = await rig(tmp_path, probe=False)
        try:
            readiness = service._feedback()
            assert readiness is None
            assert await service.download(service._jobs[0])
            assert service.executor.secured_ids() == {"picture"}
            service.tick_main()
            assert shown(service) == []
            assert service._feedback() is None
            assert await service.probe_time()
            service.tick_main()
            readiness = service._feedback()
            assert readiness is not None and readiness.secured == ("picture",)
        finally:
            await close(service)
    asyncio.run(check())


def test_centrals_clock_step_re_anchors_and_layer_positions_follow(tmp_path):
    """Mutation: sample never re-anchors -> the position and timebase stand still."""
    wall = Rig(tmp_path)
    wall.play(layer(video=True, end=130))
    drawn = wall.renderer.outputs["hdmi1"]
    before = drawn.layers[0].position

    wall.clock.step_utc(5)      # the wall reading stands for Central's time here
    wall.sync_clock()
    wall.executor.tick()

    after = wall.renderer.outputs["hdmi1"]
    assert after.timebase == pytest.approx(drawn.timebase + 5)
    assert after.layers[0].position == pytest.approx(before + 5)


def test_round_trip_jitter_inside_the_interval_never_moves_the_timebase(tmp_path):
    """A still is drawn once: equal timebases over 100 samples of varying round trips and server
    reading points. Mutation: sample always re-anchors -> the timebase moves."""
    wall = Rig(tmp_path)
    wall.play(layer())
    first = wall.renderer.outputs["hdmi1"].timebase
    for index in range(100):
        rtt = .002 + (index % 11) * .004
        read_at = .1 + (index % 7) / 6 * .8
        sent = wall.clock.monotonic()
        wall.clock.advance(rtt * read_at)
        server_time = wall.clock.utc()
        wall.clock.advance(rtt * (1 - read_at))
        assert wall.show_clock.sample(server_time=server_time, sent_monotonic=sent,
                                      received_monotonic=wall.clock.monotonic()) == "kept"
        wall.executor.tick()
        assert wall.renderer.outputs["hdmi1"].timebase == first


def test_a_sixty_second_gap_keeps_grants_and_the_started_layer_and_accepts_a_later_commit(tmp_path):
    """Mutation: grants end on the sample's age -> the grant ends and the Commit is refused."""
    wall = Rig(tmp_path)
    wall.play(layer(end=300))
    wall.advance(60, refresh=False)
    wall.executor.tick()
    assert [local.layer.assignment_id for local in wall.renderer.outputs["hdmi1"].layers] == ["picture"]
    assert wall.executor.readiness().failures == ()

    later = layer("later", start=162, end=300, media_origin=162, data=b"later", priority=5)
    wall.offer(layer(end=300), later, revision=2)
    wall.secure("later", b"later")
    wall.executor.prepare_imminent()
    wall.commit("picture", "later")
    wall.advance(2, refresh=False)
    wall.executor.tick()
    assert "later" in [local.layer.assignment_id for local in wall.renderer.outputs["hdmi1"].layers]


def test_a_day_without_a_sample_ends_no_grant_and_the_next_photo_starts(tmp_path):
    """The 2026-11-15 freeze replayed on the executor: a day of drift (8.6 s of bound) used to
    end every grant and code Readiness `clock`; now the bound is only reported, the granted next
    photo starts at its instant and no `clock` failure is ever raised. Mutation: restore the
    clock gate -> "next" never starts."""
    wall = Rig(tmp_path)
    day = 86_400
    picture = layer(end=100 + day + 100)
    wall.offer(picture, valid_until=100 + day + 100)
    wall.secure()
    wall.executor.prepare_imminent()
    wall.commit("picture")
    wall.executor.tick()

    wall.advance(day, refresh=False)
    assert wall.show_clock.uncertainty == pytest.approx(DRIFT_RATE * day)
    upcoming = layer("next", start=100 + day + 2, end=100 + day + 100, media_origin=100 + day + 2,
                     data=b"next", priority=5)
    wall.offer(picture, upcoming, revision=2, valid_until=100 + day + 100)
    wall.secure("next", b"next")
    wall.executor.prepare_imminent()
    wall.commit("picture", "next")
    readiness = wall.executor.readiness()
    assert readiness.failures == () and readiness.lost_grants == ()
    assert readiness.clock_uncertainty == pytest.approx(DRIFT_RATE * day)
    wall.advance(2, refresh=False)
    wall.executor.tick()
    assert "next" in [local.layer.assignment_id for local in wall.renderer.outputs["hdmi1"].layers]
