"""Current-authority readiness projection against real PostgreSQL state."""

from psycopg.types.json import Jsonb
from support.frames import frame_create
from support.player_state import heard_in
from support.position import PORTRAIT_ON_WIDE, save_position
from test_registry import enroll

from central.coordination import Coordinator
from central.infra.player_contact_store import player_reports_in
from central.readiness_diagnostics import project_readiness_diagnostics
from contracts.liveness import SILENT_AFTER_SECONDS
from contracts.models import Failure, Layer, Plan, Readiness


def _seed(registry):
    identity, key, request = enroll(registry, count=2)
    player_id, epoch = identity["player_id"], identity["authority_epoch"]
    for index in range(2):
        frame_id = f"diagnostic-frame-{index}"
        registry.create_frame(frame_create(frame_id), by="console")
        output_id = f"HDMI-A-{index + 1}"
        registry.bind(frame_id, player_id, output_id, expected_generation=0, by="console")
        save_position(registry, frame_id, PORTRAIT_ON_WIDE)
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
            "INSERT INTO plan_offers(player_id,authority_epoch,revision,plan_id,manifest,groups,"
            "created_at,valid_until,created_by,changed_at,changed_by) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'scheduler',%s,'scheduler')",
            (plan.player_id, plan.authority_epoch, plan.revision, plan.plan_id,
             Jsonb(plan.model_dump(mode="json")), Jsonb({layer.assignment_id: "group" for layer in layers}),
             plan.issued_at, plan.valid_until, plan.issued_at),
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
        lost_grants=(),
        failures=tuple(failures),
    )
    with registry.db.transaction() as conn:
        # As Coordination writes it: on the show state `_seed`'s configuration made, this epoch's.
        conn.execute(
            "UPDATE player_show_state SET readiness_sequence=%s,readiness_received_at=%s,"
            "readiness=%s,changed_at=%s,changed_by='node' WHERE player_id=%s AND authority_epoch=%s",
            (sequence, registry.clock.utc(), Jsonb(report.model_dump(mode="json")),
             registry.clock.utc(), plan.player_id, plan.authority_epoch),
        )
        heard_in(conn, plan.player_id, plan.authority_epoch, registry.clock.utc())


def _project(registry):
    with registry.db.transaction() as conn:
        inventory = registry.inventory_in(conn, registry.clock.utc())
        return project_readiness_diagnostics(conn, read_at=registry.clock.utc(),
                                             frames=inventory.frames,
                                             heard=player_reports_in(conn, registry.clock.utc()))


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
    registry.unbind(layers[0].frame_id, expected_generation=1, by="console")
    assert _project(registry) == ()


def test_projection_suppresses_a_silent_players_failures_and_keeps_a_heard_ones(registry):
    """Silence is the Player's liveness (when Central last heard from it), never its readiness's
    age: readiness is sent once per change, so a heard Player's report stays current however old.
    Mutation probe: judge silence by `received_at` again -> the old report of a heard Player is
    dropped."""
    identity, bindings, layers, _, _ = _seed(registry)
    plan = _offer(registry, identity, bindings, layers)
    _report(registry, plan, [Failure(assignment_id=layers[0].assignment_id, code="decode")])
    registry.clock.advance(40)
    with registry.db.transaction() as conn:
        heard_in(conn, identity["player_id"], identity["authority_epoch"], registry.clock.utc())
    assert len(_project(registry)) == 1
    with registry.db.transaction() as conn:
        conn.execute("UPDATE player_contacts SET heard_at=%s WHERE player_id=%s",
                     (registry.clock.utc() - SILENT_AFTER_SECONDS - 1, identity["player_id"]))
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
