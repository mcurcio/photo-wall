"""PostgreSQL regressions for content locks across Player authority epochs."""

import pytest
from psycopg.types.json import Jsonb
from test_coordination import publish_fixture_catalog as _publish_catalog
from test_coordination import schedule
from test_registry import enroll, frame

from central.coordination import CoordinationError, CoordinationLimits, Coordinator
from central.registry import RegistryError
from contracts.models import Calibration, Readiness


def _player_with_frame(registry, frame_id="frame-0"):
    player, key, _ = enroll(registry, count=1)
    frame(registry, frame_id)
    registry.bind(frame_id, player["player_id"], "HDMI-A-1", expected_generation=0)
    registry.calibrate(frame_id, "commit", 1, Calibration(), expected_generation=1)
    return player, key, frame_id


def _empty_catalog(registry):
    with registry.db.transaction() as conn:
        conn.execute(
            "UPDATE catalog_snapshots SET snapshot=jsonb_set(snapshot,'{candidates}',%s::jsonb) "
            "WHERE source_ref='library:1'",
            (Jsonb([]),),
        )


def _schedule_media(coordinator, starts=1010):
    schedule(coordinator, ["frame-0"], starts=starts, media=True)


def _feedback(coordinator, player, plan, *, secured=(), prepared=(), capacity_ok=True, sequence=1):
    return Readiness(
        plan_id=plan.plan_id,
        revision=plan.revision,
        authority_epoch=player["authority_epoch"],
        sequence=sequence,
        secured=tuple(secured),
        prepared=tuple(prepared),
        capacity_ok=capacity_ok,
        clock_uncertainty=0.01,
        observed_at=coordinator.clock.utc(),
    )


@pytest.mark.parametrize("feedback_kind", ["offered", "secured", "committed"])
@pytest.mark.parametrize("catalog_change", ["changed", "empty"])
def test_same_key_reenrollment_retains_unexpired_content_but_requires_fresh_epoch_feedback(
    registry,
    feedback_kind,
    catalog_change,
):
    player, key, _ = _player_with_frame(registry)
    original = _publish_catalog(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    _schedule_media(coordinator)
    old_plan = coordinator.delivery(player["player_id"], 1)["plan"]
    old_layer = old_plan.layers[0]
    old_token = player["token"]

    registry.clock.advance(6)  # The scheduled assignment is now imminent.
    if feedback_kind != "offered":
        old_feedback = _feedback(
            coordinator,
            player,
            old_plan,
            secured=(old_layer.assignment_id,),
            prepared=(old_layer.assignment_id,) if feedback_kind == "committed" else (),
            capacity_ok=feedback_kind == "committed",
        )
        assert coordinator.readiness(player["player_id"], old_feedback)
        with registry.db.transaction() as conn:
            old_commits = conn.execute("SELECT count(*) AS n FROM execution_commits").fetchone()[
                "n"
            ]
            assert bool(old_commits) == (feedback_kind == "committed")

    if catalog_change == "changed":
        _publish_catalog(registry, digest="b" * 64, asset="original-b")
    else:
        _empty_catalog(registry)

    fresh = enroll(registry, key, count=1)[0]
    assert fresh["player_id"] == player["player_id"]
    assert fresh["authority_epoch"] == 2
    with pytest.raises(RegistryError, match="unauthorized"):
        registry.authenticate(old_token)
    with pytest.raises(RegistryError, match="stale_authority"):
        coordinator.delivery(player["player_id"], 1)
    stale = _feedback(coordinator, player, old_plan, secured=(old_layer.assignment_id,))
    with pytest.raises(CoordinationError, match="stale_authority"):
        coordinator.readiness(player["player_id"], stale)

    coordinator.advance()
    current = coordinator.delivery(fresh["player_id"], fresh["authority_epoch"])
    new_plan = current["plan"]
    assert new_plan is not None
    assert new_plan.authority_epoch == 2
    assert new_plan.layers == old_plan.layers
    assert current["commits"] == ()

    fresh_layer = new_plan.layers[0]
    fresh_feedback = _feedback(
        coordinator,
        fresh,
        new_plan,
        secured=(fresh_layer.assignment_id,),
        prepared=(fresh_layer.assignment_id,),
    )
    assert coordinator.readiness(fresh["player_id"], fresh_feedback)
    assert coordinator.delivery(fresh["player_id"], 2)["commits"]
    assert original.sha256 == fresh_layer.variant.sha256


def test_old_epoch_offer_does_not_consume_current_epoch_offer_budget(registry):
    player, key, _ = _player_with_frame(registry)
    old_variant = _publish_catalog(registry)
    coordinator = Coordinator(registry.db, registry.clock, CoordinationLimits(max_offers=1))
    _schedule_media(coordinator)
    old_plan = coordinator.delivery(player["player_id"], 1)["plan"]

    _publish_catalog(registry, digest="b" * 64, asset="original-b")
    fresh = enroll(registry, key, count=1)[0]
    coordinator.advance()
    current = coordinator.delivery(fresh["player_id"], fresh["authority_epoch"])
    assert current["plan"] is not None
    assert current["plan"].authority_epoch == 2
    assert current["plan"].revision == 1
    assert current["plan"].layers[0].variant == old_variant
    assert old_plan.layers[0].variant.sha256 == old_variant.sha256


def test_expired_old_epoch_content_is_not_reused(registry):
    player, key, _ = _player_with_frame(registry)
    old_variant = _publish_catalog(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    _schedule_media(coordinator)
    old_plan = coordinator.delivery(player["player_id"], 1)["plan"]

    # Reach the actual lease boundary; old layers must no longer constrain
    # the next execution, even though the Run itself remains active.
    registry.clock.advance(old_plan.valid_until - registry.clock.utc() + 1)
    _publish_catalog(registry, digest="b" * 64, asset="original-b")
    fresh = enroll(registry, key, count=1)[0]
    coordinator.advance()
    current = coordinator.delivery(fresh["player_id"], 2)["plan"]
    assert current is not None
    assert current.layers[0].variant.sha256 == "b" * 64
    assert current.layers[0] != old_plan.layers[0]
    assert old_plan.layers[0].variant.sha256 == old_variant.sha256


def test_changed_binding_generation_cannot_reuse_old_epoch_content(registry):
    player, key, _ = _player_with_frame(registry)
    old_variant = _publish_catalog(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    _schedule_media(coordinator)
    old_plan = coordinator.delivery(player["player_id"], 1)["plan"]

    other, _, _ = enroll(registry, count=1)
    registry.bind("frame-0", other["player_id"], "HDMI-A-1", expected_generation=1)
    registry.calibrate("frame-0", "commit", 2, Calibration(), expected_generation=2)
    registry.bind("frame-0", player["player_id"], "HDMI-A-1", expected_generation=2)
    registry.calibrate("frame-0", "commit", 3, Calibration(), expected_generation=3)
    _publish_catalog(registry, digest="b" * 64, asset="original-b")
    fresh = enroll(registry, key, count=1)[0]

    coordinator.advance()
    current = coordinator.delivery(fresh["player_id"], fresh["authority_epoch"])["plan"]
    assert current is not None
    assert current.layers[0].binding_generation == 3
    assert current.layers[0].variant.sha256 == "b" * 64
    assert current.layers[0] != old_plan.layers[0]
    assert old_plan.layers[0].variant.sha256 == old_variant.sha256
