"""Real PostgreSQL coordination with explicit clocks; no physical rendering claim."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from psycopg.types.json import Jsonb
from test_registry import enroll, frame

from central.catalog import Candidate, CatalogSnapshot
from central.coordination import CoordinationError, CoordinationLimits, Coordinator
from central.registry import RegistryError
from central.runtime import Child, Contribution, Program, Scene
from central.runtime_store import RuntimeStore
from contracts.models import Calibration, Failure, Readiness, Variant


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
    candidate = Candidate(asset_id=asset, kind="image", original_width=1080, original_height=1920,
                          captured_at=registry.clock.utc(), variant=variant)
    snapshot = CatalogSnapshot(source_ref="library:1", refreshed_at=registry.clock.utc(), candidates=(candidate,))
    # These are metadata-only repository fixtures; this test does not claim real files/acquisition.
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO media_blobs VALUES(%s,%s,10,'ready',%s)",
                     (digest, Jsonb(variant.model_dump(mode="json")), registry.clock.utc()))
        conn.execute("INSERT INTO catalog_snapshots VALUES(%s,%s) ON CONFLICT(source_ref) "
                     "DO UPDATE SET snapshot=EXCLUDED.snapshot",
                     (snapshot.source_ref, Jsonb(snapshot.model_dump(mode="json"))))
    return variant


def test_offers_protect_possible_secured_bytes_across_restart_and_new_query_membership(registry):
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
        assert conn.execute("SELECT count(*) AS n FROM execution_events WHERE kind='offer_backpressure'").fetchone()["n"] == 1


def test_missing_ready_blob_refuses_whole_offer_but_persists_current_runtime(registry):
    player = setup_players(registry, count=1)[0]
    publish_fixture_catalog(registry)
    with registry.db.transaction() as conn:
        conn.execute("UPDATE media_blobs SET state='corrupt'")
    coordinator = Coordinator(registry.db, registry.clock)
    schedule(coordinator, ["frame-0"], starts=1000, media=True)
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
