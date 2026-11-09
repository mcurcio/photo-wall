"""The Player follows Central's time on its own monotonic clock (player/show_clock.py).

Through the real Executor and PlayerService: the 2026-11-08 first boot (a Pi's wall clock 975,402 s
behind Central's, every plan refused, the Frame black), and what must hold around it: a wall-clock
step changes nothing, Central's own step re-anchors, jitter never redraws a still, a session gap
keeps the picture, and only long silence ends the grants.
"""

from __future__ import annotations

import asyncio
import math

import pytest
from test_executor import Rig, layer
from test_player_service import Server, close, rig

from contracts.models import Commit, LostGrant
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
    assert not show.anchored and not show.healthy()
    assert show.now() == -math.inf and show.timebase() == -math.inf
    assert show.uncertainty == math.inf
    assert show.diagnostics.status == "no_sample"
    assert show.diagnostics.uncertainty is None     # JSON-safe


@pytest.mark.parametrize("bad", [{"max_uncertainty": 0}, {"drift_rate": -1}, {"max_uncertainty": math.inf},
                                 {"drift_rate": math.nan}])
def test_thresholds_must_be_finite_and_positive(bad):
    with pytest.raises(ValueError):
        ShowClock(lambda: 0.0, **bad)


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


def test_a_sample_too_slow_to_measure_keeps_the_anchor_and_dispersion_grows():
    clock, show = clock_at()
    clock.advance(10)
    timebase, earlier = show.timebase(), show.uncertainty
    assert show.sample(server_time=999, sent_monotonic=10, received_monotonic=10.21) == "too_slow"
    assert show.timebase() == timebase
    clock.advance(1)
    assert show.uncertainty > earlier
    assert show.diagnostics.rejected == 1


def test_a_kept_sample_reports_its_far_edge_never_less_than_the_true_bound():
    """The estimate is inside the interval but off its midpoint; the true error can be as much as
    the far edge, so the far edge is what is reported. Mutation: report rtt / 2 -> this fails."""
    clock, show = clock_at()
    clock.advance(10.05)
    # Central read at 110.01, so at receipt its time is in [110.01, 110.06]; the estimate is 110.05.
    assert show.sample(server_time=110.01, sent_monotonic=10, received_monotonic=10.05) == "kept"
    assert show.uncertainty == pytest.approx(.04)
    assert show.timebase() == 100


def test_a_sample_that_cannot_be_kept_within_bounds_re_anchors():
    """A far edge past max_uncertainty is no longer a usable bound, even inside the interval."""
    clock, show = clock_at()
    clock.advance(10.2)
    assert show.sample(server_time=110, sent_monotonic=10, received_monotonic=10.2) == "anchored"
    assert show.timebase() == pytest.approx(110.1 - 10.2)


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
            readiness, _ = service._feedback()
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
            _, observations = service._feedback()
            assert [(o.status, o.observed_at) for o in observations] == [("presented", pytest.approx(102))]
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
            readiness, _ = service._feedback()
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
            readiness, _ = service._feedback()
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
            readiness, _ = service._feedback()
            assert readiness is None
            assert await service.download(service._jobs[0])
            assert service.executor.secured_ids() == {"picture"}
            service.tick_main()
            assert shown(service) == []
            assert service._feedback()[0] is None
            assert await service.probe_time()
            service.tick_main()
            readiness, _ = service._feedback()
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
    """Mutation: healthy() requires age <= 30 -> the grant ends and the Commit is refused."""
    wall = Rig(tmp_path)
    wall.play(layer(end=300))
    wall.advance(60, refresh=False)
    wall.executor.tick()
    assert [local.layer.assignment_id for local in wall.renderer.outputs["hdmi1"].layers] == ["picture"]
    assert wall.show_clock.healthy()
    assert wall.executor.readiness().failures == ()

    later = layer("later", start=162, end=300, media_origin=162, data=b"later", priority=5)
    wall.offer(layer(end=300), later, revision=2)
    wall.secure("later", b"later")
    wall.executor.prepare_imminent()
    wall.commit("picture", "later")
    wall.advance(2, refresh=False)
    wall.executor.tick()
    assert "later" in [local.layer.assignment_id for local in wall.renderer.outputs["hdmi1"].layers]


def test_sixteen_minutes_without_a_sample_ends_grants_and_one_sample_recovers(tmp_path):
    """About 16 minutes of drift passes MAX_UNCERTAINTY: grants end, Readiness says `clock`, and
    what already started keeps showing. Mutation: uncertainty ignores age -> still healthy."""
    wall = Rig(tmp_path)
    picture = layer(end=3000)
    wall.offer(picture, valid_until=3000)
    wall.secure()
    wall.executor.prepare_imminent()
    wall.commit("picture")
    wall.executor.tick()

    wall.advance(999, refresh=False)
    assert wall.show_clock.healthy()
    upcoming = layer("next", start=1102, end=3000, media_origin=1102, data=b"next", priority=5)
    wall.offer(picture, upcoming, revision=2, valid_until=3000)
    wall.secure("next", b"next")
    wall.executor.prepare_imminent()
    commit = wall.commit("picture", "next")

    wall.advance(2, refresh=False)      # past MAX_UNCERTAINTY / DRIFT_RATE = 1000 s
    assert not wall.show_clock.healthy()
    wall.executor.tick()
    assert {failure.code for failure in wall.executor.readiness().failures} == {"clock"}
    wall.advance(2, refresh=False)      # "next"'s instant passes with its grant gone
    wall.executor.tick()
    assert [local.layer.assignment_id for local in wall.renderer.outputs["hdmi1"].layers] == ["picture"]

    wall.sync_clock()
    assert wall.show_clock.healthy()
    wall.executor.prepare_imminent()
    readiness = wall.executor.readiness()
    assert readiness.failures == ()
    assert LostGrant(assignment_id="next", readiness_sequence=commit.readiness_sequence) in readiness.lost_grants
