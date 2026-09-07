"""Evidence correlation faults; synthetic records do not qualify a native VM."""

from copy import deepcopy

import pytest

from scripts.vm_media_evidence import presentation, read_grants, read_presentations


@pytest.fixture
def record():
    variant = dict(sha256="a" * 64, size=100, media_type="image/jpeg", width=72, height=128)
    layer = dict(assignment_id="a1", run_id="r1", output_id="Virtual-1", frame_id="f1",
                 binding_generation=1, start=10, end=70, media_origin=10, variant=variant)
    binding = dict(output_id="Virtual-1", frame_id="f1", generation=1,
                   profile=dict(width_px=640, height_px=480, diagonal_inches=10))
    return dict(
        player_id="p1", received_at=21, layer=layer,
        plan=dict(plan_id="plan1", player_id="p1", authority_epoch=1, revision=2,
                  issued_at=0, valid_from=0, valid_until=80, bindings=[binding], layers=[layer]),
        configuration=dict(player_id="p1", authority_epoch=1, configuration_revision=1,
                           bindings=[binding], enabled_outputs=["Virtual-1"]),
        readiness=dict(plan_id="plan1", revision=2, authority_epoch=1, sequence=4,
                       secured=["a1"], prepared=["a1"], capacity_ok=True,
                       clock_uncertainty=.01, observed_at=19),
        observation=dict(plan_id="plan1", revision=2, authority_epoch=1,
                         assignment_id="a1", observed_at=20, status="presented"),
        variant=variant, blob_digest="a" * 64, blob_size=100, blob_state="ready",
        job_digest="a" * 64, job_state="ready",
        commit=dict(authority_epoch=1, revision=2, assignment_id="a1", group_id="g1",
                    valid=True, committed_at=18, readiness_sequence=3),
        group=dict(id="g1", status="committed", members={"a1": ["p1", 1, "Virtual-1", 1]},
                   starts_at=10, valid_until=70),
    )


def proof(record, **kwargs):
    return presentation(record, player_id="p1", authority_epoch=1,
                        frame_id="f1", output_id="Virtual-1", **kwargs)


def test_exact_photo_has_separate_current_and_commit_readiness_sequences(record):
    record["observation"]["handling"] = {
        "planner": "record_observation",
        "runtime": "record_observation",
    }
    result = proof(record)
    assert result["sha256"] == "a" * 64
    assert result["readiness_sequence"] == 4
    assert result["commit_readiness_sequence"] == 3
    assert result["assignment_id"] == "a1"
    assert not {"variant", "configuration", "readiness", "plan", "native_rendering"} & result.keys()


@pytest.mark.parametrize("section,key,value", [
    ("observation", "status", "fallback"), ("observation", "assignment_id", "different"),
    ("observation", "authority_epoch", 2), ("observation", "revision", 1),
    ("observation", "plan_id", "different"), ("observation", "observed_at", 70),
    ("observation", "observed_at", 9), ("observation", "detail", "decode"),
    ("readiness", "prepared", []), ("readiness", "secured", []),
    ("readiness", "capacity_ok", False), ("readiness", "clock_uncertainty", 1),
    ("readiness", "sequence", 2), ("readiness", "revision", 3),
    ("commit", "valid", False), ("commit", "committed_at", 21),
    ("commit", "readiness_sequence", True), ("commit", "group_id", "other"),
    ("group", "status", "skipped"), ("group", "members", {"a1": ["p1", 2, "Virtual-1", 1]}),
    ("configuration", "enabled_outputs", []), ("configuration", "authority_epoch", 2),
])
def test_mismatched_or_unqualified_execution_cannot_become_proof(record, section, key, value):
    record[section][key] = value
    assert proof(record) is None


@pytest.mark.parametrize("key,value", [
    ("blob_digest", "b" * 64), ("blob_size", 99), ("blob_state", "corrupt"),
    ("job_state", "running"), ("job_digest", "b" * 64), ("player_id", "other"),
    ("received_at", 19),
])
def test_unprepared_bytes_or_wrong_player_do_not_count(record, key, value):
    record[key] = value
    assert proof(record) is None


def test_restart_must_match_previously_observed_bytes(record):
    assert proof(record, expected_sha256="b" * 64) is None
    assert proof(record, expected_sha256="a" * 64) is not None


def test_first_photo_is_bound_to_worker_original_and_not_another_favorite(record):
    record["original_sha256"] = "b" * 64
    assert proof(record, expected_original_sha256="c" * 64) is None
    result = proof(record, expected_original_sha256="b" * 64)
    assert result["original_sha256"] == "b" * 64
    assert result["sha256"] == "a" * 64  # Converted JPEG may differ from its source.


def test_commit_renewal_requires_a_captured_grant_before_the_drawing(record):
    grant = dict(record["commit"], player_id="p1", plan_id="plan1")
    record["commit"]["committed_at"] = 21
    assert proof(record) is None
    assert proof(record, prior_grants=(grant,))["committed_at"] == 18
    for key, value in (("player_id", "other"), ("plan_id", "other"), ("authority_epoch", 2),
                       ("assignment_id", "other"), ("revision", 1), ("group_id", "other"), ("valid", False)):
        assert proof(record, prior_grants=(dict(grant, **{key: value}),)) is None
    record["commit"]["valid"] = False
    assert proof(record, prior_grants=(grant,)) is None


def test_same_assignment_name_with_changed_plan_content_is_rejected(record):
    record["plan"] = deepcopy(record["plan"])
    record["plan"]["layers"][0]["variant"]["sha256"] = "b" * 64
    assert proof(record) is None


def test_database_join_against_production_coordination_records(registry):
    from psycopg.types.json import Jsonb
    from test_coordination import publish_fixture_catalog, report, schedule, setup_players

    from central.coordination import Coordinator
    from contracts.models import Observation

    player = setup_players(registry, count=1)[0]
    variant = publish_fixture_catalog(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    schedule(coordinator, ["frame-0"], media=True)
    registry.clock.advance(6)
    coordinator.advance()
    ready = report(coordinator, player)
    coordinator.readiness(player["player_id"], ready)
    coordinator.advance()
    plan = coordinator.delivery(player["player_id"], 1)["plan"]
    assignment = next(layer for layer in plan.layers if layer.start == 1010)
    registry.clock.advance(5)
    # Synthetic observation exercises evidence SQL only; this is no renderer proof.
    coordinator.observe(player["player_id"], Observation(
        plan_id=plan.plan_id, revision=plan.revision, authority_epoch=1,
        assignment_id=assignment.assignment_id, observed_at=1011, status="presented"))
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO media_sources(source_ref,spec) VALUES('library:1',%s)", (Jsonb({}),))
        conn.execute("INSERT INTO source_members SELECT 'library:1',asset_id FROM asset_revisions")
        arguments = dict(player_id=player["player_id"], authority_epoch=1, frame_id="frame-0",
                         output_id="HDMI-A-1", source_ref="library:1")
        results = read_presentations(conn, **arguments)
        assert results and results[0]["sha256"] == variant.sha256
        grants = read_grants(conn, player_id=player["player_id"], authority_epoch=1)
        conn.execute("UPDATE execution_commits SET committed_at=1012")
        assert read_presentations(conn, **arguments) == []
        assert read_presentations(conn, **arguments, prior_grants=tuple(grants))
        conn.execute("UPDATE execution_commits SET valid=FALSE")
        assert read_presentations(conn, **arguments) == []
