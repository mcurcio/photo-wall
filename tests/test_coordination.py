"""Real PostgreSQL coordination with explicit clocks; no physical rendering claim."""

import math
from concurrent.futures import ThreadPoolExecutor

import pytest
from support.frame_media import seed_variant
from support.house_time import UTC_HOUSE, daily, set_house, window_program
from support.key_content_builders import face
from support.measured_photos import measure, measure_all
from support.photo_pools import photo, seed_pool
from support.position import save_position
from test_registry import SKEWED, enroll, frame

from central.coordination import CoordinationError, CoordinationLimits, Coordinator
from central.registry import RegistryError
from central.runtime import Child, Contribution, Program, ProgramDraft, Scene
from central.runtime_store import RuntimeStore
from central.wall_plan.selection import SceneSelection
from contracts.models import Failure, Readiness


def setup_players(registry, count=2):
    players = []
    for i in range(count):
        player, key, _ = enroll(registry)
        frame_id = f"frame-{i}"
        frame(registry, frame_id)
        registry.bind(frame_id, player["player_id"], "HDMI-A-1", expected_generation=0)
        save_position(registry, frame_id)
        players.append(player)
    return players


def future_program(coordinator, program_id, scene_id, starts, ends):
    """A Program whose first window starts `starts - now` seconds from now, as a Program created
    at `starts`' distance before it: windows start on a minute, so the start moves up to the
    minute and the test clock moves by the same amount first. Returns that shift (seconds)."""
    aligned = math.ceil(starts / 60) * 60
    shift = aligned - starts
    set_house(coordinator.db, UTC_HOUSE)
    coordinator.clock.advance(shift)
    coordinator.runtime.command_current(
        "create_program", window_program(program_id, scene_id, aligned, math.ceil(ends / 60) * 60))
    return shift


def schedule(coordinator, frames, *, starts=1010, media=False):
    """The Scene "scheduled" on `frames` from `starts`: Show now at `starts` when it is not in the
    future, else a Program (`future_program`: a start of 1010 becomes 1020, the clock at 1010)."""
    coordinator.runtime.command("set_scene", Scene(scene_id="scheduled", loop=True, cycle_seconds=60,
        contributions=tuple(Contribution(target=f"frame:{f}", kind="media" if media else "black",
                                         source_refs=("library:1",) if media else ()) for f in frames)))
    if starts <= coordinator.clock.utc():
        coordinator.runtime.command("activate", "scheduled", "calendar", starts)
    else:
        future_program(coordinator, "calendar", "scheduled", starts, 2000)
    return coordinator.advance()


def report(coordinator, player, *, sequence=1, prepared=True):
    plan = coordinator.delivery(player["player_id"], player["authority_epoch"])["plan"]
    imminent = tuple(a.assignment_id for a in plan.layers if a.start <= coordinator.clock.utc() + 5
                     and a.end > coordinator.clock.utc())
    return Readiness(plan_id=plan.plan_id, revision=plan.revision, authority_epoch=plan.authority_epoch,
                     sequence=sequence, secured=imminent, prepared=imminent if prepared else (),
                     clock_uncertainty=.01, capacity_ok=True, observed_at=coordinator.clock.utc())


def test_runtime_concurrent_idempotent_admission_and_restart(registry):
    store = RuntimeStore(registry.db, registry.clock)
    store.command("set_scene", Scene(scene_id="scene", loop=True))
    def activate(_):
        return RuntimeStore(registry.db, registry.clock).command("activate", "scene", "same-event", 1000)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(activate, range(4)))
    assert len({r.run_id for r in results}) == 1
    restarted = RuntimeStore(registry.db, registry.clock)
    assert restarted.read().project(1020).runs[0].run_id == results[0].run_id
    before = restarted.read().export_state()
    restarted.read().project(1500)
    assert restarted.read().export_state() == before
    with pytest.raises(ValueError):
        restarted.command("activate", "missing", "different-event", 1000)
    assert restarted.read().export_state() == before


def test_configuration_revision_detects_independent_output_changes(registry):
    player, key, _ = enroll(registry)
    for i in range(2):
        frame(registry, f"f{i}")
        registry.bind(f"f{i}", player["player_id"], f"HDMI-A-{i+1}", expected_generation=0)
    coordinator = Coordinator(registry.db, registry.clock)
    first = coordinator.configuration(player["player_id"], 1)
    assert len(first.bindings) == 2 and first.enabled_outputs == ()
    for revision in range(1, 5):
        save_position(registry, "f0")
    high = coordinator.configuration(player["player_id"], 1)
    assert high.configuration_revision == first.configuration_revision + 1
    save_position(registry, "f1", SKEWED)
    low = coordinator.configuration(player["player_id"], 1)
    assert low.configuration_revision == high.configuration_revision + 1
    assert coordinator.configuration(player["player_id"], 1) == low
    fresh, _, _ = enroll(registry, key)
    with pytest.raises(RegistryError, match="stale_authority"):
        coordinator.configuration(player["player_id"], 1)
    assert coordinator.configuration(fresh["player_id"], 2).configuration_revision == 1


def test_all_required_players_prepare_before_atomic_commit_and_future_loss_skips_group(registry):
    players = setup_players(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    schedule(coordinator, ["frame-0", "frame-1"])
    registry.clock.advance(6)
    for player in players:
        assert coordinator.delivery(player["player_id"], 1)["commits"] == ()
    first = report(coordinator, players[0])
    assert coordinator.readiness(players[0]["player_id"], first)
    assert coordinator.delivery(players[0]["player_id"], 1)["commits"] == ()
    assert coordinator.readiness(players[1]["player_id"], report(coordinator, players[1]))
    assert all(coordinator.delivery(p["player_id"], 1)["commits"] for p in players)
    assert not coordinator.readiness(players[0]["player_id"], first)
    lost = report(coordinator, players[0], sequence=2, prepared=False)
    coordinator.readiness(players[0]["player_id"], lost)
    for player in players:
        delivery = coordinator.delivery(player["player_id"], 1)
        assert delivery["commits"] == ()
        assert len(delivery["revocations"]) == 1
        assert delivery["revocations"][0].mode == "invalidate"


def test_missing_frame_stays_required_and_deadline_skips_instead_of_partial_commit(registry):
    player = setup_players(registry, count=1)[0]
    coordinator = Coordinator(registry.db, registry.clock)
    projection = schedule(coordinator, ["frame-0", "unbound"])
    assert any(d.code == "frame_unbound" for d in projection.diagnostics)
    registry.clock.advance(6)
    coordinator.readiness(player["player_id"], report(coordinator, player))
    registry.clock.advance(4)
    coordinator.advance()
    delivery = coordinator.delivery(player["player_id"], 1)
    assert delivery["commits"] == () and delivery["revocations"]


def test_late_join_has_bounded_preparation_grace_and_keeps_logical_origin(registry):
    player = setup_players(registry, count=1)[0]
    coordinator = Coordinator(registry.db, registry.clock)
    schedule(coordinator, ["frame-0"], starts=990)
    registry.clock.advance(2)
    ready = report(coordinator, player)
    coordinator.readiness(player["player_id"], ready)
    delivery = coordinator.delivery(player["player_id"], 1)
    assert delivery["commits"]
    first = delivery["plan"].layers[0]
    assert first.start == first.media_origin == 990 and first.position(1002) == 12


def publish_fixture_catalog(registry, digest="a" * 64, asset="original-a", *, also=()):
    """Source `library:1`'s pool becomes exactly one photo (`asset`), prepared as `digest`: a
    complete check of the pool through the store's worker side, its key content measured
    (roadmap 5a: the Planner places only measured photos), then its media variant made under the
    wall's current recipe (coordination hydrates variants from their records; the wall needs its
    Frames first). `also` names more (asset, digest) photos of the same pool, prepared the same
    way: one photo is never on two Frames at once (roadmap 5a), so a Scene on N Frames needs N
    photos. Returns the first photo's variant."""
    # 4K portrait: a 9:16 Frame full screen (Cover) on the enrolled 1920 x 1080 Output draws
    # its picture at 1920 x 3414 (its Frame output, follow-ups B1), which this photo holds.
    pool_photo = photo(asset, taken_at=registry.clock.utc(), width=2160, height=3840)
    others = [(photo(label, taken_at=registry.clock.utc(), width=2160, height=3840), other)
              for label, other in also]
    seed_pool(registry, "library:1", (pool_photo, *(item for item, _ in others)))
    measure_all(registry, (pool_photo, *(item for item, _ in others)))
    for item, other in others:
        seed_variant(registry, item.asset_id, other)
    return seed_variant(registry, pool_photo.asset_id, digest)


def test_a_scene_naming_two_sources_plays_only_their_pools_and_asks_for_their_photos(registry):
    """F-1: a contribution's photos are the union of the pools of the Sources it names, whether or
    not each pool's check completed (C7); a pool it does not name never plays."""
    setup_players(registry, count=1)
    # 4K portrait: what the 9:16 Frame on the 1920 x 1080 Output needs (its Frame output).
    family = (photo("family", taken_at=900, width=2160, height=3840),
              photo("shared", taken_at=950, width=2160, height=3840))
    garden = (family[1], photo("garden", taken_at=990, width=2160, height=3840))
    seed_pool(registry, "family:1", family)
    seed_pool(registry, "garden:1", garden, complete=False)
    seed_pool(registry, "other:1", (photo("other", taken_at=999),))
    measure_all(registry, (*family, garden[1], photo("other", taken_at=999)))
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", Scene(scene_id="two", loop=True, cycle_seconds=60,
        contributions=(Contribution(target="frame:frame-0", kind="media",
                                    source_refs=("family:1", "garden:1")),)))
    coordinator.runtime.command("activate", "two", "two-start", 1000)
    projection = coordinator.advance()
    union = {asset.asset_id for asset in family + garden}
    # Nothing is prepared yet, so the preparation horizon requests them all and the hour's
    # plan beyond it places them tentatively; the pool it does not name never plays.
    planned = [entry.asset_id for entry in projection.plan if entry.asset_id is not None]
    assert set(planned) == union and len(planned) > len(union)
    assert {request.asset_id for request in projection.acquisitions} == union
    assert "source_missing" not in {d.code for d in projection.diagnostics}


def test_offers_protect_possible_secured_bytes_across_central_restart_and_new_query_membership(registry):
    player = setup_players(registry, count=1)[0]
    original = publish_fixture_catalog(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    schedule(coordinator, ["frame-0"], starts=1000, media=True)
    offered = coordinator.delivery(player["player_id"], 1)["plan"]
    first = offered.layers[0]
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM media_references").fetchone()["n"] > 0
    registry.clock.advance(31)  # Missing 30-second ACK is not permission to reroll.
    publish_fixture_catalog(registry, digest="b" * 64, asset="original-b")
    restarted = Coordinator(registry.db, registry.clock)
    restarted.advance()
    newer = restarted.delivery(player["player_id"], 1)["plan"]
    retained = next(a for a in newer.layers if a.assignment_id == first.assignment_id)
    assert retained.variant == original
    delayed = Readiness(plan_id=offered.plan_id, revision=offered.revision, authority_epoch=1,
                        sequence=1, secured=(first.assignment_id,), capacity_ok=False,
                        observed_at=registry.clock.utc(), clock_uncertainty=.01)
    assert restarted.readiness(player["player_id"], delayed)
    with registry.db.transaction() as conn:
        stored = conn.execute("SELECT layer FROM assignment_locks WHERE assignment_id=%s",
                              (first.assignment_id,)).fetchone()
        assert stored["layer"]["variant"]["sha256"] == original.sha256
    assert restarted.delivery(player["player_id"], 1)["commits"] == ()


def test_stale_generation_and_unknown_feedback_cannot_create_locks(registry):
    players = setup_players(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    schedule(coordinator, ["frame-0"])
    registry.clock.advance(6)
    ready = report(coordinator, players[0])
    bogus = ready.model_copy(update={"failures": (Failure(assignment_id="unknown", code="decode"),)})
    with pytest.raises(CoordinationError, match="unknown_assignment"):
        coordinator.readiness(players[0]["player_id"], bogus)
    registry.bind("frame-0", players[1]["player_id"], "HDMI-A-2", expected_generation=1)
    with pytest.raises(CoordinationError, match="stale_binding"):
        coordinator.readiness(players[0]["player_id"], ready)
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM assignment_locks").fetchone()["n"] == 0
    assert coordinator.delivery(players[0]["player_id"], 1)["plan"] is None


def test_offer_bound_applies_backpressure_without_stopping_runtime(registry):
    player = setup_players(registry, count=1)[0]
    coordinator = Coordinator(registry.db, registry.clock, CoordinationLimits(max_offers=1))
    schedule(coordinator, ["frame-0"], starts=1000)
    original = coordinator.delivery(player["player_id"], 1)["plan"]
    registry.clock.advance(61)
    coordinator.advance()
    assert coordinator.delivery(player["player_id"], 1)["plan"] == original
    assert coordinator.runtime.read().export_state()["now"] == registry.clock.utc()
    with registry.db.transaction() as conn:
        event = conn.execute(
            "SELECT detail FROM execution_events WHERE kind='offer_backpressure'"
        ).fetchone()
        assert event["detail"]["handling"] == {
            "runtime": "preserve_lifecycle",
            "planner": "await_capacity",
        }


def test_missing_ready_blob_refuses_whole_offer_but_persists_current_runtime(registry):
    player = setup_players(registry, count=1)[0]
    publish_fixture_catalog(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", Scene(scene_id="scheduled", loop=True, cycle_seconds=60,
        contributions=(Contribution(target="frame:frame-0", kind="media", source_refs=("library:1",)),)))
    coordinator.runtime.command("activate", "scheduled", "calendar", 1000)
    original_offer = coordinator._offer

    def corrupt_after_planning(conn, plan, groups):
        conn.execute("UPDATE assets SET produced_size=NULL,produced_sha256=NULL "
                     "WHERE kind='media-variant'")
        original_offer(conn, plan, groups)

    # The catalog is hydrated before _offer.  Exercise the publication race so
    # a blob disappearing at that boundary refuses the whole offer atomically.
    coordinator._offer = corrupt_after_planning
    coordinator.advance()
    assert coordinator.delivery(player["player_id"], 1)["plan"] is None
    assert coordinator.runtime.read().project(1000).runs
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM media_references").fetchone()["n"] == 0


def test_mixed_calibrated_outputs_use_complete_plan_configuration_and_suppress_stale_calibration(registry):
    player = setup_players(registry, count=1)[0]
    frame(registry, "review-required")
    registry.bind("review-required", player["player_id"], "HDMI-A-2", expected_generation=0)
    coordinator = Coordinator(registry.db, registry.clock)
    schedule(coordinator, ["frame-0"])
    delivered = coordinator.delivery(player["player_id"], 1)
    assert len(delivered["plan"].bindings) == 2
    assert delivered["plan"].bindings == delivered["configuration"].bindings
    registry.clock.advance(6)
    ready = report(coordinator, player)
    coordinator.readiness(player["player_id"], ready)
    assert coordinator.delivery(player["player_id"], 1)["commits"]
    save_position(registry, "frame-0", SKEWED)   # a new Position: the offered plan is stale
    stale = coordinator.delivery(player["player_id"], 1)
    assert stale["plan"] is None and stale["commits"] == ()
    coordinator.readiness(player["player_id"], ready.model_copy(update={"sequence": 2}))
    assert coordinator.delivery(player["player_id"], 1)["commits"] == ()
    coordinator.advance()
    fresh = coordinator.delivery(player["player_id"], 1)
    assert fresh["plan"].bindings == fresh["configuration"].bindings
    assert fresh["plan"].revision > delivered["plan"].revision


def test_prepared_feedback_after_normal_deadline_cannot_create_late_grant(registry):
    player = setup_players(registry, count=1)[0]
    coordinator = Coordinator(registry.db, registry.clock)
    schedule(coordinator, ["frame-0"])
    registry.clock.advance(11)
    coordinator.readiness(player["player_id"], report(coordinator, player))
    delivery = coordinator.delivery(player["player_id"], 1)
    assert not delivery["commits"] and delivery["revocations"]


def start_nested_missing_cue(coordinator):
    root = Scene(scene_id="nested", loop=True, cycle_seconds=60,
        contributions=(Contribution(target="frame:frame-0", kind="black"),),
        children=(Child(scene=Scene(scene_id="short", cycle_seconds=10,
            contributions=(Contribution(target="frame:missing", kind="black"),))),))
    coordinator.runtime.command("set_scene", root)
    coordinator.runtime.command("activate", "nested", "nested-start", 1000)
    coordinator.advance()


def test_failed_short_child_expiry_never_narrows_skipped_required_group_into_success(registry):
    player = setup_players(registry, count=1)[0]
    coordinator = Coordinator(registry.db, registry.clock)
    start_nested_missing_cue(coordinator)
    registry.clock.advance(5)
    coordinator.advance()
    skipped = coordinator.delivery(player["player_id"], 1)["revocations"][0]
    registry.clock.advance(6)
    coordinator.advance()
    coordinator.readiness(player["player_id"], report(coordinator, player))
    delivery = coordinator.delivery(player["player_id"], 1)
    assert not delivery["commits"]
    assert any(r.assignment_ids == skipped.assignment_ids for r in delivery["revocations"])
    with registry.db.transaction() as conn:
        cues = conn.execute("SELECT status,members FROM coordination_groups WHERE starts_at=1000").fetchall()
        assert len(cues) == 1 and cues[0]["status"] == "skipped" and len(cues[0]["members"]) == 2


def test_explicit_binding_cohort_change_offers_fresh_revision_for_current_state(registry):
    player = setup_players(registry, count=1)[0]
    coordinator = Coordinator(registry.db, registry.clock)
    start_nested_missing_cue(coordinator)
    registry.clock.advance(5)
    coordinator.advance()
    old = coordinator.delivery(player["player_id"], 1)
    assert old["revocations"]
    frame(registry, "missing")
    registry.bind("missing", player["player_id"], "HDMI-A-2", expected_generation=0)
    save_position(registry, "missing")
    registry.clock.advance(1)
    coordinator.advance()
    current = coordinator.delivery(player["player_id"], 1)
    assert current["plan"].revision > old["plan"].revision and not current["revocations"]
    assert current["plan"].layers[0].media_origin == 1000
    coordinator.readiness(player["player_id"], report(coordinator, player))
    assert len(coordinator.delivery(player["player_id"], 1)["commits"][0].assignment_ids) == 2


# Pending cues are forecasts: an operator edit that rewrites the forecast supersedes them.

def ending_scene(scene_id="ending"):
    return Scene(scene_id=scene_id, loop=True, cycle_seconds=60, outro_seconds=10,
        contributions=(Contribution(target="frame:frame-0", kind="black"),),
        outro_contributions=(Contribution(target="frame:frame-0", kind="black", opacity=.5),))


def cohorts(registry):
    with registry.db.transaction() as conn:
        return conn.execute("SELECT id,cue_key,starts_at,members,status FROM coordination_groups "
                            "ORDER BY starts_at,cohort_sequence").fetchall()


def skip_codes(registry):
    with registry.db.transaction() as conn:
        return {row["detail"]["group_id"]: row["detail"]["code"] for row in conn.execute(
            "SELECT detail FROM execution_events WHERE kind='group_skipped'").fetchall()}


def assert_outro_plays_after_finish(registry, coordinator, player, start=1000):
    """The Run started at `start` (cycle 60) was finished 20 s in: its outro plays at start + 60."""
    coordinator.advance()  # the edit's own tick: the grown cue no longer aborts coordination
    outro = next(layer for layer in coordinator.delivery(player["player_id"], 1)["plan"].layers
                 if layer.start == start + 60)
    assert (outro.end, outro.opacity) == (start + 70, .5)
    codes = skip_codes(registry)
    old, new = [g for g in cohorts(registry) if g["starts_at"] == start + 60]
    assert codes[old["id"]] == "superseded" and new["status"] == "pending"
    assert list(new["members"]) == [outro.assignment_id]
    registry.clock.advance(start + 56 - registry.clock.utc())
    coordinator.advance()
    coordinator.readiness(player["player_id"], report(coordinator, player))
    committed = {a for c in coordinator.delivery(player["player_id"], 1)["commits"] for a in c.assignment_ids}
    assert outro.assignment_id in committed
    roots = [run for run in coordinator.runtime.read().project(start + 60).runs if run.parent_id is None]
    assert roots[0].phase == "outro"


def test_operator_finish_with_outro_supersedes_the_grouped_next_cycle(registry):
    player = setup_players(registry, count=1)[0]
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", ending_scene())
    run_id = coordinator.runtime.command("activate", "ending", "go", 1000).run_id
    coordinator.advance()
    registry.clock.advance(20)
    coordinator.runtime.command_current("finish", run_id)
    assert_outro_plays_after_finish(registry, coordinator, player)


def test_removing_a_running_program_with_outro_supersedes_the_grouped_next_cycle(registry):
    player = setup_players(registry, count=1)[0]
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", ending_scene())
    set_house(registry.db, UTC_HOUSE)
    coordinator.runtime.command_current("create_program", window_program("p", "ending", 1020, 4980))
    registry.clock.advance(20)
    coordinator.advance()                       # the window starts at 1020
    registry.clock.advance(20)
    coordinator.runtime.command_current("remove_program", "p", 1)
    assert_outro_plays_after_finish(registry, coordinator, player, start=1020)


def test_scene_edit_for_an_upcoming_program_supersedes_each_affected_cue_once(registry):
    players = setup_players(registry, count=2)
    coordinator = Coordinator(registry.db, registry.clock)
    one = Scene(scene_id="s", loop=True, cycle_seconds=60,
                contributions=(Contribution(target="frame:frame-0", kind="black"),))
    coordinator.runtime.command("set_scene", one)
    future_program(coordinator, "p", "s", 1100, 1500)
    coordinator.advance()
    before = cohorts(registry)
    assert before and all(len(g["members"]) == 1 for g in before)
    coordinator.runtime.command("set_scene", one.model_copy(update={"revision": 2, "contributions": (
        *one.contributions, Contribution(target="frame:frame-1", kind="black"))}))
    coordinator.advance()
    after, codes = cohorts(registry), skip_codes(registry)
    for old in before:
        cue = [g for g in after if g["cue_key"] == old["cue_key"]]
        assert cue[0]["id"] == old["id"] and len(cue) == 2
        assert cue[0]["status"] == "skipped" and codes[old["id"]] == "superseded"
        assert cue[1]["status"] == "pending" and len(cue[1]["members"]) == 2
    assert {layer.start for layer in coordinator.delivery(players[1]["player_id"], 1)["plan"].layers} \
        == {g["starts_at"] for g in before}
    coordinator.advance()
    assert cohorts(registry) == after  # a steady forecast reuses the new cohorts
    coordinator.runtime.command("set_scene", one.model_copy(update={"revision": 3}))
    coordinator.advance()  # back to the first membership: a third cohort, not the first one
    for old in before:
        cue = [g for g in cohorts(registry) if g["cue_key"] == old["cue_key"]]
        assert [g["status"] for g in cue] == ["skipped", "skipped", "pending"]
        assert cue[2]["members"].keys() == old["members"].keys()


def test_a_started_pending_cue_never_narrows_into_success(registry):
    player = setup_players(registry, count=1)[0]
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", Scene(scene_id="nested", loop=True, cycle_seconds=60,
        contributions=(Contribution(target="frame:frame-0", kind="black"),),
        children=(Child(scene=Scene(scene_id="blink", cycle_seconds=1,
            contributions=(Contribution(target="frame:missing", kind="black"),))),)))
    coordinator.runtime.command("activate", "nested", "nested-start", 1000)
    coordinator.advance()
    registry.clock.advance(2)
    coordinator.advance()
    coordinator.readiness(player["player_id"], report(coordinator, player))
    assert not coordinator.delivery(player["player_id"], 1)["commits"]
    assert [g["status"] for g in cohorts(registry) if g["starts_at"] == 1000] == ["pending"]


def drive(registry, coordinator, players, until, *, step=4, watch=None, since=0):
    """Tick and report readiness like live Players; return instants a watched Frame was dark.

    A step within prepare_seconds gives every cue a tick inside its commit window."""
    dark = []
    while registry.clock.utc() < until:
        registry.clock.advance(step)
        coordinator.advance()
        for sequence, player in enumerate(players):
            coordinator.readiness(player["player_id"], report(
                coordinator, player, sequence=int(registry.clock.utc() * 10) + sequence))
        now = registry.clock.utc()
        for player in (watch or ()) if now >= since else ():
            delivery = coordinator.delivery(player["player_id"], 1)
            live = {a for c in delivery["commits"] for a in c.assignment_ids}
            live -= {a for r in delivery["revocations"] for a in r.assignment_ids}
            if not any(layer.assignment_id in live for layer in delivery["plan"].layers
                       if layer.start <= now < layer.end):
                dark.append((player["player_id"], now))
    return dark


def group_events(registry, group_id):
    with registry.db.transaction() as conn:
        return [row["detail"] for row in conn.execute(
            "SELECT detail FROM execution_events WHERE kind='group_skipped' "
            "AND detail->>'group_id'=%s", (group_id,)).fetchall()]


def two_frame_scene():
    return Scene(scene_id="scheduled", revision=2, loop=True, cycle_seconds=60,
                 contributions=tuple(Contribution(target=f"frame:frame-{i}", kind="black") for i in (0, 1)))


def test_an_edit_inside_the_prepare_window_supersedes_a_committed_cue_and_both_frames_show_it(registry):
    players = setup_players(registry, count=2)
    coordinator = Coordinator(registry.db, registry.clock, CoordinationLimits(horizon_seconds=60))
    schedule(coordinator, ["frame-0"])
    drive(registry, coordinator, players, 1016, step=2)
    committed = next(g for g in cohorts(registry) if g["starts_at"] == 1020)
    assert committed["status"] == "committed"
    coordinator.runtime.command("set_scene", two_frame_scene())
    dark = drive(registry, coordinator, players, 1022, step=2, watch=players, since=1020)
    dark += drive(registry, coordinator, players, 1078, watch=players)
    assert dark == [], "no Frame goes dark in the cycle"
    detail = group_events(registry, committed["id"])
    assert [(d["code"], d["cue_key"], d["root"] is not None) for d in detail] == [
        ("superseded", committed["cue_key"], True)]
    assert [g["status"] for g in cohorts(registry) if g["cue_key"] == committed["cue_key"]] \
        == ["skipped", "committed"]


def test_a_player_that_fetches_the_superseding_plan_after_the_start_still_commits_it(registry):
    players = setup_players(registry, count=2)
    coordinator = Coordinator(registry.db, registry.clock, CoordinationLimits(horizon_seconds=60))
    schedule(coordinator, ["frame-0"])
    drive(registry, coordinator, players, 1016, step=2)
    committed = next(g for g in cohorts(registry) if g["starts_at"] == 1020)
    assert committed["status"] == "committed"  # frame-0 starts 1020 on this commit, offline
    coordinator.runtime.command("set_scene", two_frame_scene())
    for _ in range(5):  # 1017..1021: the scheduler ticks; neither Player fetches or reports
        registry.clock.advance(1)
        coordinator.advance()
    for sequence, player in enumerate(players, start=20000):  # past drive()'s sequences
        assert coordinator.readiness(player["player_id"], report(coordinator, player, sequence=sequence))
    assert "not_ready" not in skip_codes(registry).values()
    for player in players:
        delivery = coordinator.delivery(player["player_id"], 1)
        first = next(layer for layer in delivery["plan"].layers if layer.start == 1020)
        assert first.media_origin == 1020 and not delivery["revocations"]
        assert first.assignment_id in {a for c in delivery["commits"] for a in c.assignment_ids}


def test_a_started_cue_that_grows_is_skipped_while_other_frames_keep_their_offers(registry):
    players = setup_players(registry, count=3)
    coordinator = Coordinator(registry.db, registry.clock)
    schedule(coordinator, ["frame-0", "frame-1"])
    drive(registry, coordinator, players[:2], 1022, step=2)
    started = next(g for g in cohorts(registry) if g["starts_at"] == 1020)
    assert started["status"] == "committed"
    # No Runtime edit grows a started cue; forge the stored cohort narrower to reach the rule.
    frame_1 = next(layer.assignment_id for layer in coordinator.delivery(
        players[1]["player_id"], 1)["plan"].layers if layer.start == 1020)
    with registry.db.transaction() as conn:
        conn.execute("UPDATE coordination_groups SET members=members-%s WHERE id=%s",
                     (frame_1, started["id"]))
    coordinator.runtime.command("set_scene", Scene(scene_id="other", loop=True, cycle_seconds=60,
        contributions=(Contribution(target="frame:frame-2", kind="black"),)))
    coordinator.runtime.command("activate", "other", "other-now", registry.clock.utc())
    coordinator.advance()
    [detail] = group_events(registry, started["id"])
    assert (detail["code"], detail["cue_key"]) == ("cue_membership_changed", started["cue_key"])
    assert detail["root"]
    assert coordinator.delivery(players[0]["player_id"], 1)["commits"] == ()
    assert coordinator.delivery(players[0]["player_id"], 1)["revocations"]
    assert coordinator.delivery(players[2]["player_id"], 1)["plan"].layers
    coordinator.advance()  # the skipped cue stays skipped: no event and revocation per tick
    assert len(group_events(registry, started["id"])) == 1


def test_a_new_binding_supersedes_the_pending_cohort_it_replaces(registry):
    player = setup_players(registry, count=1)[0]
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", Scene(scene_id="s", loop=True, cycle_seconds=60,
        contributions=(Contribution(target="frame:frame-0", kind="black"),
                       Contribution(target="frame:late", kind="black"))))
    shift = future_program(coordinator, "p", "s", 1100, 1200)
    coordinator.advance()
    before = next(g for g in cohorts(registry) if g["starts_at"] == 1100 + shift)
    frame(registry, "late")
    registry.bind("late", player["player_id"], "HDMI-A-2", expected_generation=0)
    save_position(registry, "late")
    registry.clock.advance(90)
    drive(registry, coordinator, [player], 1110 + shift)
    assert [d["code"] for d in group_events(registry, before["id"])] == ["superseded"]
    assert "not_ready" not in skip_codes(registry).values()
    assert [g["status"] for g in cohorts(registry) if g["cue_key"] == before["cue_key"]] \
        == ["skipped", "committed"]


@pytest.mark.parametrize("edit_at", [1000, 1098])
def test_moving_an_upcoming_program_end_earlier_plans_its_outro(registry, edit_at):
    player = setup_players(registry, count=1)[0]
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", ending_scene())
    shift = future_program(coordinator, "p", "ending", 1100, 1500)      # 1140 to 1500
    old = coordinator.runtime.read().export_state()["programs"]["p"]
    coordinator.advance()
    edit_at += shift
    registry.clock.advance(max(0, edit_at - 4 - registry.clock.utc()))  # nothing starts before 1140
    drive(registry, coordinator, [player], edit_at)
    coordinator.runtime.command_current(
        "save_program", Program.model_validate(old),
        ProgramDraft(name="p", scene_id="ending", when=daily("00:19", "00:20")))      # ends 1200
    if edit_at < 1130:
        registry.clock.advance(1130 - edit_at)
    drive(registry, coordinator, [player], 1206)
    codes = set(skip_codes(registry).values())
    assert "cue_membership_changed" not in codes and "not_ready" not in codes
    committed = {g["starts_at"] for g in cohorts(registry) if g["status"] == "committed"}
    assert {1140, 1200} <= committed  # the body cycle, then the outro at the window's end


def test_finishing_a_parent_with_children_never_freezes_or_misses_a_cue(registry):
    players = setup_players(registry, count=3)
    coordinator = Coordinator(registry.db, registry.clock, CoordinationLimits(horizon_seconds=60))
    coordinator.runtime.command("set_scene", Scene(scene_id="parent", loop=True, cycle_seconds=60,
        outro_seconds=10, contributions=(Contribution(target="frame:frame-0", kind="black"),),
        outro_contributions=(Contribution(target="frame:frame-0", kind="black", opacity=.5),),
        children=(
            Child(scene=Scene(scene_id="kid1", loop=True, cycle_seconds=20, outro_seconds=5,
                contributions=(Contribution(target="frame:frame-1", kind="black"),),
                outro_contributions=(Contribution(target="frame:frame-1", kind="black", opacity=.3),))),
            Child(delay_seconds=7, scene=Scene(scene_id="kid2", cycle_seconds=15,
                contributions=(Contribution(target="frame:frame-2", kind="black"),))),
        )))
    run_id = coordinator.runtime.command("activate", "parent", "go", 1000).run_id
    coordinator.advance()
    drive(registry, coordinator, players, 1020)
    coordinator.runtime.command_current("finish", run_id)
    drive(registry, coordinator, players, 1076)
    codes = set(skip_codes(registry).values())
    assert "cue_membership_changed" not in codes and "not_ready" not in codes
    assert any(g["starts_at"] == 1060 and g["status"] == "committed" for g in cohorts(registry))


def test_removing_a_program_inside_its_prepare_window_withdraws_its_committed_cue(registry):
    player = setup_players(registry, count=1)[0]
    coordinator = Coordinator(registry.db, registry.clock)
    schedule(coordinator, ["frame-0"])
    drive(registry, coordinator, [player], 1016, step=2)
    committed = next(g for g in cohorts(registry) if g["starts_at"] == 1020)
    assert committed["status"] == "committed"
    coordinator.runtime.command_current("remove_program", "calendar", 1)
    coordinator.advance()
    assert [d["code"] for d in group_events(registry, committed["id"])] == ["superseded"]
    assert coordinator.delivery(player["player_id"], 1)["commits"] == ()


def test_orphaned_pending_cues_are_superseded_not_skipped_as_not_ready(registry):
    setup_players(registry, count=1)
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", ending_scene())
    future_program(coordinator, "p", "ending", 1100, 1500)
    coordinator.advance()
    forecast = cohorts(registry)
    assert forecast and all(g["status"] == "pending" for g in forecast)
    coordinator.runtime.command_current("remove_program", "p", 1)
    coordinator.advance()
    codes = skip_codes(registry)
    assert all(codes[g["id"]] == "superseded" for g in forecast)
    assert all(g["status"] == "skipped" for g in cohorts(registry))
    registry.clock.advance(150)
    coordinator.advance()
    assert "not_ready" not in skip_codes(registry).values()


# Roadmap 5a (slice O1): a hand-placed photo is checked at its Scene's own crop limit. A square
# photo on a 9:16 Frame crops 43.75 % of its area: it passes at 50 % and is refused at 35 %.

def square_photo_scene(registry, crop_limit=None):
    frame(registry, "portrait")
    square = photo("square", taken_at=900, width=2000, height=2000)
    seed_pool(registry, "square:1", (square,))
    selection = {} if crop_limit is None else {"selection": SceneSelection(crop_limit=crop_limit)}
    scene = Scene(scene_id="hand-placed", contributions=(
        Contribution(target="frame:portrait", asset_refs=(square.asset_id,)),), **selection)
    return scene, square


def test_a_hand_placed_photo_is_authored_at_its_scenes_crop_limit(registry):
    scene, square = square_photo_scene(registry, crop_limit=0.5)
    coordinator = Coordinator(registry.db, registry.clock)
    result = coordinator.configure_authored_scene(scene, "square:1", (square.asset_id,))
    assert result["status"] == "configured" and result["created"] == 1


def test_a_hand_placed_photo_beyond_the_default_crop_limit_is_refused(registry):
    scene, square = square_photo_scene(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    with pytest.raises(RegistryError, match="authored_incompatible"):
        coordinator.configure_authored_scene(scene, "square:1", (square.asset_id,))


def prepare(registry, photos):
    """Each photo's prepared copy ready under the current recipe, as a worker leaves it."""
    for number, item in enumerate(photos):
        seed_variant(registry, item.asset_id, f"{number + 1:064x}", width=item.width,
                     height=item.height)


def test_two_frames_of_one_shape_show_different_photos_with_their_crops(registry):
    """Roadmap 5a (slice W1), through the real advance: the wall plan puts a photo on one Frame
    at a time, offers each layer with its crop, and records each offered photo's Scene and pass.
    A 2:3 photo with a face at its left edge is cropped to keep the left (offset 0) on these
    9:16 Frames. Owner, 2026-10-11: "Never show the same photo on two frames at the same
    time"."""
    players = setup_players(registry, count=2)
    portraits = tuple(photo(f"portrait-{n}", taken_at=900 + n, width=2400, height=3600)
                      for n in range(3))
    seed_pool(registry, "library:1", portraits)
    measure(registry, portraits[0], faces=(face(0.0, 0.3, 0.15, 0.5),))
    measure_all(registry, portraits[1:])
    prepare(registry, portraits)
    coordinator = Coordinator(registry.db, registry.clock)
    schedule(coordinator, ["frame-0", "frame-1"], starts=1000, media=True)
    plans = [coordinator.delivery(player["player_id"], 1)["plan"] for player in players]
    first = [next(layer for layer in plan.layers if layer.start == 1000) for plan in plans]
    assert first[0].variant != first[1].variant
    by_digest = {f"{n + 1:064x}": item.asset_id for n, item in enumerate(portraits)}
    offsets = {by_digest[layer.variant.sha256]: layer.crop.offset
               for plan in plans for layer in plan.layers}
    assert offsets.get(portraits[0].asset_id, 0.0) == 0.0
    assert all(layer.crop is not None for plan in plans for layer in plan.layers)
    with registry.db.transaction() as conn:
        selected = conn.execute("SELECT assignment_id,asset_id,scene_id,pass_number "
                                "FROM photo_selections ORDER BY starts_at,pass_number").fetchall()
    offered = {layer.assignment_id: by_digest[layer.variant.sha256]
               for plan in plans for layer in plan.layers}
    assert {row["assignment_id"]: row["asset_id"] for row in selected} == offered
    assert {row["scene_id"] for row in selected} == {"scheduled"}
    passes = [row["pass_number"] for row in selected]
    assert passes[0] == 1 and passes == sorted(passes)
