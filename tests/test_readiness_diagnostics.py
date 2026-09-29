"""Current-authority readiness projection against real PostgreSQL state."""

from psycopg.types.json import Jsonb
from test_registry import enroll

from central.coordination import Coordinator
from central.readiness_diagnostics import project_readiness_diagnostics
from central.registry import FrameCreate
from contracts.models import Calibration, Failure, FrameProfile, Layer, Plan, Readiness


def _seed(registry):
    identity, key, request = enroll(registry, count=2)
    player_id, epoch = identity["player_id"], identity["authority_epoch"]
    for index in range(2):
        frame_id = f"diagnostic-frame-{index}"
        registry.create_frame(FrameCreate(
            id=frame_id,
            width_mm=300,
            height_mm=500,
            profile=FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24),
        ))
        output_id = f"HDMI-A-{index + 1}"
        registry.bind(frame_id, player_id, output_id, expected_generation=0)
        registry.calibrate(frame_id, "commit", 1, Calibration(), expected_generation=1)
    bindings = tuple(registry.configuration_for(player_id, epoch)["bindings"])
    Coordinator(registry.db, registry.clock).configuration(player_id, epoch)
    layers = tuple(
        Layer(
            assignment_id=f"diagnostic-assignment-{index}",
            run_id="diagnostic-run",
            output_id=binding.output_id,
            frame_id=binding.frame_id,
            binding_generation=binding.generation,
            start=990,
            end=1100,
            media_origin=990,
            presentation="black",
        )
        for index, binding in enumerate(bindings)
    )
    return identity, bindings, layers, key, request.device_id


def _offer(registry, identity, bindings, layers, *, revision=1, valid_until=1200):
    plan = Plan(
        plan_id=f"diagnostic-plan-{revision}",
        revision=revision,
        player_id=identity["player_id"],
        authority_epoch=identity["authority_epoch"],
        issued_at=900,
        valid_from=900,
        valid_until=valid_until,
        bindings=bindings,
        layers=layers,
    )
    with registry.db.transaction() as conn:
        conn.execute(
            "INSERT INTO plan_offers VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (plan.player_id, plan.authority_epoch, plan.revision, plan.plan_id,
             Jsonb(plan.model_dump(mode="json")), Jsonb({layer.assignment_id: "group" for layer in layers}),
             plan.issued_at, plan.valid_until),
        )
    return plan


def _report(registry, plan, failures, *, sequence=1):
    report = Readiness(
        plan_id=plan.plan_id,
        revision=plan.revision,
        authority_epoch=plan.authority_epoch,
        sequence=sequence,
        capacity_ok=True,
        clock_uncertainty=0.01,
        observed_at=registry.clock.utc(),
        failures=tuple(failures),
    )
    with registry.db.transaction() as conn:
        conn.execute(
            "INSERT INTO player_feedback VALUES(%s,%s,%s,%s,%s) "
            "ON CONFLICT(player_id,authority_epoch) DO UPDATE SET sequence=EXCLUDED.sequence,"
            "received_at=EXCLUDED.received_at,readiness=EXCLUDED.readiness",
            (plan.player_id, plan.authority_epoch, sequence, registry.clock.utc(),
             Jsonb(report.model_dump(mode="json"))),
        )


def _project(registry):
    with registry.db.transaction() as conn:
        inventory = registry.inventory_in(conn, registry.clock.utc())
        return project_readiness_diagnostics(conn, read_at=registry.clock.utc(), frames=inventory.frames)


def test_projection_resolves_only_failed_assignment_across_multiple_outputs(registry):
    identity, bindings, layers, _, _ = _seed(registry)
    plan = _offer(registry, identity, bindings, layers)
    _report(registry, plan, [Failure(assignment_id=layers[1].assignment_id, code="decode")])

    diagnostics = _project(registry)

    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert (diagnostic.player_id, diagnostic.authority_epoch) == (
        identity["player_id"], identity["authority_epoch"]
    )
    assert (diagnostic.assignment_id, diagnostic.frame_id, diagnostic.output_id) == (
        layers[1].assignment_id, layers[1].frame_id, layers[1].output_id
    )
    assert diagnostic.failure_code == "decode"
    assert diagnostic.layer_start == layers[1].start and diagnostic.layer_end == layers[1].end


def test_projection_suppresses_failure_from_superseded_offer(registry):
    identity, bindings, layers, _, _ = _seed(registry)
    old = _offer(registry, identity, bindings, layers, revision=1)
    _offer(registry, identity, bindings, layers, revision=2)
    _report(registry, old, [Failure(assignment_id=layers[0].assignment_id, code="capacity")])

    assert _project(registry) == ()


def test_projection_suppresses_expired_latest_offer_instead_of_reviving_old_offer(registry):
    identity, bindings, layers, _, _ = _seed(registry)
    old = _offer(registry, identity, bindings, layers, revision=1)
    expired_layers = tuple(layer.model_copy(update={"end": 998}) for layer in layers)
    _offer(registry, identity, bindings, expired_layers, revision=2, valid_until=999)
    _report(registry, old, [Failure(assignment_id=layers[0].assignment_id, code="decode")])

    assert _project(registry) == ()


def test_projection_suppresses_stale_frame_generation(registry):
    identity, bindings, layers, _, _ = _seed(registry)
    plan = _offer(registry, identity, bindings, layers)
    _report(registry, plan, [Failure(assignment_id=layers[0].assignment_id, code="decode")])
    registry.unbind(layers[0].frame_id, expected_generation=1)
    assert _project(registry) == ()


def test_projection_suppresses_silent_feedback(registry):
    identity, bindings, layers, _, _ = _seed(registry)
    plan = _offer(registry, identity, bindings, layers)
    _report(registry, plan, [Failure(assignment_id=layers[0].assignment_id, code="decode")])
    with registry.db.transaction() as conn:
        conn.execute("UPDATE player_feedback SET received_at=%s WHERE player_id=%s",
                     (registry.clock.utc() - 40, identity["player_id"]))
    assert _project(registry) == ()


def test_projection_suppresses_feedback_from_a_replaced_epoch(registry):
    identity, bindings, layers, key, device_id = _seed(registry)
    plan = _offer(registry, identity, bindings, layers)
    _report(registry, plan, [Failure(assignment_id=layers[0].assignment_id, code="decode")])

    rebooted, _, _ = enroll(registry, key=key, count=2, device_id=device_id)

    assert rebooted["player_id"] == identity["player_id"]
    assert rebooted["authority_epoch"] == identity["authority_epoch"] + 1
    assert _project(registry) == ()


def test_projection_suppresses_failures_for_ended_layers(registry):
    identity, bindings, layers, _, _ = _seed(registry)
    ended_layers = tuple(layer.model_copy(update={"end": 999}) for layer in layers)
    plan = _offer(registry, identity, bindings, ended_layers)
    _report(registry, plan, [Failure(assignment_id=ended_layers[0].assignment_id, code="decode")])

    assert _project(registry) == ()
