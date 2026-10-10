"""Real PostgreSQL coordination with explicit clocks; no physical rendering claim."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha1
from uuid import UUID

import pytest
from psycopg.types.json import Jsonb
from test_registry import enroll, frame

from central.catalog import CatalogSnapshot
from central.coordination import CoordinationError, CoordinationLimits, Coordinator
from central.registry import RegistryError
from central.runtime import Child, Contribution, Program, Scene
from central.runtime_store import RuntimeStore
from contracts.models import Calibration, Failure, Readiness, Variant
from media.models import OriginalAsset


def setup_players(registry, count=2):
    players = []
    for i in range(count):
        player, key, _ = enroll(registry)
        frame_id = f"frame-{i}"
        frame(registry, frame_id)
        registry.bind(frame_id, player["player_id"], "HDMI-A-1", expected_generation=0)
        registry.calibrate(frame_id, "commit", 1, Calibration(), expected_generation=1)
        players.append(player)
    return players


def schedule(coordinator, frames, *, starts=1010, media=False):
    coordinator.runtime.command("set_scene", Scene(scene_id="scheduled", loop=True, cycle_seconds=60,
        contributions=tuple(Contribution(target=f"frame:{f}", kind="media" if media else "black",
                                         source_refs=("library:1",) if media else ()) for f in frames)))
    coordinator.runtime.command("set_program", Program(program_id="calendar", scene_id="scheduled",
                                                        starts_at=starts, ends_at=2000))
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
        registry.calibrate("f0", "commit", revision, Calibration(), expected_generation=1)
    high = coordinator.configuration(player["player_id"], 1)
    assert high.configuration_revision == first.configuration_revision + 1
    registry.calibrate("f1", "commit", 1, Calibration(gain=.8), expected_generation=1)
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


def publish_fixture_catalog(registry, digest="a" * 64, asset="original-a"):
    variant = Variant(sha256=digest, size=10, media_type="image/jpeg", width=1080, height=1920)
    original = OriginalAsset(
        connection_id="fixture", upstream_id=str(UUID(bytes=sha1(asset.encode()).digest()[:16])),
        original_sha1=sha1(asset.encode()).hexdigest(), kind="image", raw_width=1080,
        raw_height=1920, orientation=1, captured_at=registry.clock.utc(), file_size=10)
    candidate = original.candidate.model_copy(update={"variant": variant})
    snapshot = CatalogSnapshot(source_ref="library:1", refreshed_at=registry.clock.utc(), candidates=(candidate,))
    # Seed the same durable metadata and ready recipe state that a completed
    # worker publication exposes; coordination hydrates variants through jobs.
    recipe = "f" * 64
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO media_settings(singleton,max_bytes,recipe_id) VALUES(TRUE,%s,%s) "
                     "ON CONFLICT(singleton) DO UPDATE SET recipe_id=EXCLUDED.recipe_id",
                     (4 * 1024**3, recipe))
        conn.execute("INSERT INTO asset_revisions VALUES(%s,%s,%s,%s) ON CONFLICT(asset_id) DO NOTHING",
                     (original.asset_id, Jsonb(original.model_dump(mode="json")), registry.clock.utc(),
                      registry.clock.utc()))
        conn.execute("INSERT INTO media_blobs VALUES(%s,%s,10,'ready',%s)",
                     (digest, Jsonb(variant.model_dump(mode="json")), registry.clock.utc()))
        conn.execute("INSERT INTO media_jobs(id,asset_id,recipe_id,state,earliest_start,variant_sha,updated_at) "
                     "VALUES(%s,%s,%s,'ready',%s,%s,%s)",
                     ("fixture-job-" + original.asset_id, original.asset_id, recipe,
                      registry.clock.utc(), digest, registry.clock.utc()))
        conn.execute("INSERT INTO catalog_snapshots VALUES(%s,%s) ON CONFLICT(source_ref) "
                     "DO UPDATE SET snapshot=EXCLUDED.snapshot",
                     (snapshot.source_ref, Jsonb(snapshot.model_dump(mode="json"))))
    return variant


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
    coordinator.runtime.command("set_program", Program(program_id="calendar", scene_id="scheduled",
                                                        starts_at=1000, ends_at=2000))
    original_offer = coordinator._offer

    def corrupt_after_planning(conn, plan, groups):
        conn.execute("UPDATE media_blobs SET state='corrupt'")
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
    registry.calibrate("frame-0", "preview", 2, Calibration(gain=.5), expected_generation=1)
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
    registry.calibrate("missing", "commit", 1, Calibration(), expected_generation=1)
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


def assert_outro_plays_after_finish(registry, coordinator, player):
    coordinator.advance()  # the edit's own tick: the grown 1060 cue no longer aborts coordination
    outro = next(layer for layer in coordinator.delivery(player["player_id"], 1)["plan"].layers
                 if layer.start == 1060)
    assert (outro.end, outro.opacity) == (1070, .5)
    codes = skip_codes(registry)
    old, new = [g for g in cohorts(registry) if g["starts_at"] == 1060]
    assert codes[old["id"]] == "superseded" and new["status"] == "pending"
    assert list(new["members"]) == [outro.assignment_id]
    registry.clock.advance(1056 - registry.clock.utc())
    coordinator.advance()
    coordinator.readiness(player["player_id"], report(coordinator, player))
    committed = {a for c in coordinator.delivery(player["player_id"], 1)["commits"] for a in c.assignment_ids}
    assert outro.assignment_id in committed
    assert coordinator.runtime.read().project(1060).runs[0].phase == "outro"


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
    coordinator.runtime.command("set_program", Program(program_id="p", scene_id="ending",
                                                        starts_at=1000, ends_at=5000))
    coordinator.advance()
    registry.clock.advance(20)
    coordinator.runtime.command_current("remove_program", "p")
    assert_outro_plays_after_finish(registry, coordinator, player)


def test_scene_edit_for_an_upcoming_program_supersedes_each_affected_cue_once(registry):
    players = setup_players(registry, count=2)
    coordinator = Coordinator(registry.db, registry.clock)
    one = Scene(scene_id="s", loop=True, cycle_seconds=60,
                contributions=(Contribution(target="frame:frame-0", kind="black"),))
    coordinator.runtime.command("set_scene", one)
    coordinator.runtime.command("set_program", Program(program_id="p", scene_id="s",
                                                        starts_at=1100, ends_at=1500))
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
    drive(registry, coordinator, players, 1006, step=2)
    committed = next(g for g in cohorts(registry) if g["starts_at"] == 1010)
    assert committed["status"] == "committed"
    coordinator.runtime.command("set_scene", two_frame_scene())
    dark = drive(registry, coordinator, players, 1012, step=2, watch=players, since=1010)
    dark += drive(registry, coordinator, players, 1068, watch=players)
    assert dark == [], "no Frame goes dark in the cycle"
    detail = group_events(registry, committed["id"])
    assert [(d["code"], d["cue_key"], d["root"] is not None) for d in detail] == [
        ("superseded", committed["cue_key"], True)]
    assert [g["status"] for g in cohorts(registry) if g["cue_key"] == committed["cue_key"]] \
        == ["skipped", "committed"]


def test_a_started_cue_that_grows_is_skipped_while_other_frames_keep_their_offers(registry):
    players = setup_players(registry, count=3)
    coordinator = Coordinator(registry.db, registry.clock)
    schedule(coordinator, ["frame-0", "frame-1"])
    drive(registry, coordinator, players[:2], 1012, step=2)
    started = next(g for g in cohorts(registry) if g["starts_at"] == 1010)
    assert started["status"] == "committed"
    # No Runtime edit grows a started cue; forge the stored cohort narrower to reach the rule.
    frame_1 = next(layer.assignment_id for layer in coordinator.delivery(
        players[1]["player_id"], 1)["plan"].layers if layer.start == 1010)
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
    coordinator.runtime.command("set_program", Program(program_id="p", scene_id="s",
                                                        starts_at=1100, ends_at=1200))
    coordinator.advance()
    before = next(g for g in cohorts(registry) if g["starts_at"] == 1100)
    frame(registry, "late")
    registry.bind("late", player["player_id"], "HDMI-A-2", expected_generation=0)
    registry.calibrate("late", "commit", 1, Calibration(), expected_generation=1)
    registry.clock.advance(90)
    drive(registry, coordinator, [player], 1110)
    assert [d["code"] for d in group_events(registry, before["id"])] == ["superseded"]
    assert "not_ready" not in skip_codes(registry).values()
    assert [g["status"] for g in cohorts(registry) if g["cue_key"] == before["cue_key"]] \
        == ["skipped", "committed"]


@pytest.mark.parametrize("edit_at", [1000, 1098])
def test_moving_an_upcoming_program_end_earlier_plans_its_outro(registry, edit_at):
    player = setup_players(registry, count=1)[0]
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", ending_scene())
    old = Program(program_id="p", scene_id="ending", starts_at=1100, ends_at=1500)
    coordinator.runtime.command("set_program", old)
    coordinator.advance()
    registry.clock.advance(max(0, edit_at - 4 - registry.clock.utc()))  # nothing starts before 1100
    drive(registry, coordinator, [player], edit_at)
    coordinator.runtime.command_current("replace_program", old, old.model_copy(update={"ends_at": 1130}))
    if edit_at < 1090:
        registry.clock.advance(1090 - edit_at)
    drive(registry, coordinator, [player], 1166)
    codes = set(skip_codes(registry).values())
    assert "cue_membership_changed" not in codes and "not_ready" not in codes
    committed = {g["starts_at"] for g in cohorts(registry) if g["status"] == "committed"}
    assert {1100, 1160} <= committed  # the body cycle, then the outro at the cycle end


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
    drive(registry, coordinator, [player], 1006, step=2)
    committed = next(g for g in cohorts(registry) if g["starts_at"] == 1010)
    assert committed["status"] == "committed"
    coordinator.runtime.command_current("remove_program", "calendar")
    coordinator.advance()
    assert [d["code"] for d in group_events(registry, committed["id"])] == ["superseded"]
    assert coordinator.delivery(player["player_id"], 1)["commits"] == ()


def test_orphaned_pending_cues_are_superseded_not_skipped_as_not_ready(registry):
    setup_players(registry, count=1)
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", ending_scene())
    coordinator.runtime.command("set_program", Program(program_id="p", scene_id="ending",
                                                        starts_at=1100, ends_at=1500))
    coordinator.advance()
    forecast = cohorts(registry)
    assert forecast and all(g["status"] == "pending" for g in forecast)
    coordinator.runtime.command_current("remove_program", "p")
    coordinator.advance()
    codes = skip_codes(registry)
    assert all(codes[g["id"]] == "superseded" for g in forecast)
    assert all(g["status"] == "skipped" for g in cohorts(registry))
    registry.clock.advance(150)
    coordinator.advance()
    assert "not_ready" not in skip_codes(registry).values()
