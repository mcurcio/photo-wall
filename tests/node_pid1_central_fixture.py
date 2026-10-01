"""Standalone PID1 companion: real Central owners; explicitly synthetic qualification.

Only disposable schemas and supplied exact archives. No production certification,
process/display witness injection, operator reboot, or bound withdrawal.
"""

from __future__ import annotations

import json
import secrets
import shutil
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

import uvicorn
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from test_fleet_attempts import DEVICE_ID, SERIAL
from test_fleet_rollout_gate import _certificate, _gate, _LocalImageVerifier
from test_node_boot import seed_verified_publication

from central.app import create_app
from central.assets.layout import CacheLayout
from central.assets.store import CacheStore
from central.content_wiring import build_content_services
from central.fleet.acceptance_evidence import current_control_receipt_matches
from central.fleet.acceptance_query import load_current_app_control_in
from central.fleet.node_acceptance import current_cohort_in
from central.fleet.node_app_links import load_current_node_app_link_in
from central.fleet.node_boot import NodeBootService, NodeDeployment
from central.fleet.node_lifecycle import OperatorAppStage
from central.fleet.node_sessions import NodeControlConfig, NodeControlError, command_eligibility_in
from central.infra.asset_records import PgAssetRecords
from central.infra.transactions import PgTransactions
from central.kernel.assets import AssetKey, AssetKind
from central.registry import Registry
from central.transaction_locks import acquire_runtime_locks
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_boot import NodeBaseRefV2, NodeBootRequestV2, encode_node_boot_offer
from contracts.node_lifecycle import parse_app_effect_event, parse_stage_command
from contracts.player_control import ControlAppliedReceipt
from contracts.time import SystemClock

# The outage begins once Central has served the target artifact, its last contribution to a
# switch, and lasts at least OUTAGE_SECONDS and until the probe has watched the node converge
# (POST /fixture/outage-release), never longer than OUTAGE_CAP_SECONDS.
OUTAGE_SECONDS = 35
OUTAGE_CAP_SECONDS = 300


@contextmanager
def central_fixture(registry, components_dir, extra_refs_and_archives, workdir, max_boots=1):
    # Own the listening socket before any fallible publication/cache setup.
    with socket.socket() as listener:
        listener.bind(("0.0.0.0", 0))
        listener.listen(16)
        with _central_fixture(
            registry, components_dir, extra_refs_and_archives, workdir, listener, max_boots
        ) as fixture:
            yield fixture


@contextmanager
def _central_fixture(
    registry, components_dir, extra_refs_and_archives, workdir, listener, max_boots
):
    """Yield real HTTP origins plus fixture token; caller owns random-schema registry.

    extras maps phase -> (AppEnvironmentRefV2 or reference dict, exact archive).
    max_boots bounds the distinct kernel boots of the one device (2 for a reboot).
    Component provenance/base identity here is a fixture, never a PXE release claim.
    """
    components_dir, workdir = Path(components_dir), Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    clock = SystemClock()
    registry = Registry(registry.db, clock)
    components = json.loads((components_dir / "components.json").read_text())
    manager = AppEnvironmentRefV2(**components["manager_primary"])
    original = AppEnvironmentRefV2(**components["app_environment"])
    refs = {"cold": original}
    archives = {
        original.environment_sha256: components_dir / "app.tar",
        manager.environment_sha256: components_dir / "manager-primary.tar",
    }
    for phase, (reference, archive) in extra_refs_and_archives.items():
        reference = AppEnvironmentRefV2(**reference) if isinstance(reference, dict) else reference
        refs[phase] = reference
        archives[reference.environment_sha256] = Path(archive)
    references = {r.environment_sha256: r for r in [manager, *refs.values()]}
    store = CacheStore(CacheLayout(workdir / "cache"))
    records, transactions = PgAssetRecords(clock), PgTransactions(registry.db)
    # Copy and independently measure exact fixture archives before serving them.
    for digest, source in archives.items():
        reference = references[digest]
        facts = store.measure(source)
        if (facts.sha256, facts.size) != (digest, reference.size_bytes):
            raise ValueError("fixture_archive_reference_mismatch")
        key = AssetKey(AssetKind.SEALED_ENVIRONMENT, digest)
        target = store.layout.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    port = listener.getsockname()[1]
    origin, host_origin = f"http://host.docker.internal:{port}", f"http://127.0.0.1:{port}"
    token = secrets.token_hex(32)
    content = build_content_services(registry.db, clock, cache_root=workdir / "cache")
    app = create_app(
        registry.db,
        clock,
        token,
        run_scheduler=True,
        media_root=workdir / "media",
        content=content,
        mdns_enabled=False,
        node_control=NodeControlConfig("pid1-real-central-fixture"),
        node_serving_verifier=_LocalImageVerifier(),
    )
    sessions, lifecycle = app.state.node_sessions, app.state.node_lifecycle
    protocol_refusals = {}
    refusal_lock = threading.Lock()
    # Observe sanitized refusal codes without changing any production predicate.
    def observe_refusals(name, method):
        def observed(*args, **kwargs):
            try:
                return method(*args, **kwargs)
            except NodeControlError as error:
                with refusal_lock:
                    key = name + ":" + error.code
                    if len(protocol_refusals) < 32 or key in protocol_refusals:
                        protocol_refusals[key] = protocol_refusals.get(key, 0) + 1
                raise
        return observed
    lifecycle.effect = observe_refusals("effect", lifecycle.effect)
    boots = NodeBootService(sessions)
    base = NodeBaseRefV2(
        "v99.0.0",
        "8" * 64,
        "9" * 64,
        1024,
        original.base_abi,
        original.graphics_abi,
        original.plugin_abi,
    )
    deployments = {}
    for phase, ref in refs.items():
        selected = NodeDeployment(
            uuid4(),
            base,
            ref,
            manager,
            None,
            {
                r.environment_sha256: origin + "/fixture/origin/" + r.environment_sha256
                for r in [manager, ref]
            },
        )
        seed_verified_publication(
            registry, selected
        )  # Explicit synthetic release-provenance fixture.
        boots.publish(selected)
        deployments[phase] = selected
    boots.select(deployments["cold"].deployment_id, 0)
    for digest, path in archives.items():
        with transactions.begin() as tx:
            records.record_produced(
                tx, AssetKey(AssetKind.SEALED_ENVIRONMENT, digest), store.measure(path)
            )
    gate, verifier = _gate(registry, _certificate(expires_in=300))
    generation = gate.open(expected_revision=0).generation
    operations = {}
    # The outage phase drops every node exchange once the target artifact is in flight: the
    # switch must converge locally and report afterwards.
    dropped = {"outage": 0}
    outage = {"started": None, "released": False, "last_drop": None}
    state_lock = threading.Lock()
    boot_requests = {}  # kernel boot id -> the PXE boot request of that boot

    def authenticate(request):
        if not secrets.compare_digest(request.headers.get("X-Fixture-Token", ""), token):
            raise HTTPException(403, "fixture_token_required")

    def current_in(conn):
        row = conn.execute(
            "SELECT session_id FROM node_sessions WHERE device_id=%s "
            "AND owner='app_effect_broker' AND revoked_at IS NULL",
            (DEVICE_ID,),
        ).fetchone()
        if row is None:
            raise NodeControlError("fixture_broker_not_enrolled", 409)
        principal = sessions.load_operator_target_in(conn, row["session_id"])
        link = load_current_node_app_link_in(conn, principal)
        if link is None:
            raise NodeControlError("fixture_real_app_link_pending", 409)
        return principal, link

    def outage_active(now):
        started = outage["started"]
        if started is None or now >= started + OUTAGE_CAP_SECONDS:
            return False
        return not outage["released"] or now < started + OUTAGE_SECONDS

    @app.middleware("http")
    async def central_outage_after_accept(request, call_next):
        entry = operations.get("outage")
        path = request.url.path
        if entry and path.startswith("/v2/node/") and outage_active(clock.utc()):
            dropped["outage"] += 1
            outage["last_drop"] = clock.utc()
            return JSONResponse({"fixture": "central_unreachable"}, status_code=503)
        response = await call_next(request)
        if (entry and outage["started"] is None and path.startswith("/v2/node/app-attempts/")
                and path.endswith("/artifacts/target")):
            # The stage is accepted and its target is streaming: nothing else needs Central.
            outage["started"] = clock.utc()
        return response

    @app.post("/fixture/outage-release")
    def outage_release(request: Request):
        authenticate(request)
        if outage["started"] is None:
            raise HTTPException(409, "fixture_outage_not_started")
        outage["released"] = True
        return {"released": True}

    @app.post("/fixture/node-ready")
    def node_ready(request: Request, value: dict):
        authenticate(request)
        supplied = UUID(value["boot_id"])
        with state_lock:
            boot_request = boot_requests.get(supplied)
            if boot_request is None:
                if len(boot_requests) >= max_boots:
                    raise HTTPException(409, "fixture_boot_changed")
                boot_request = NodeBootRequestV2(SERIAL, supplied, secrets.token_hex(32))
                boot_requests[supplied] = boot_request
            offer = boots.offer(boot_request)
        return {
            "offer": json.loads(encode_node_boot_offer(offer)),
            "central": origin,
            "serial": SERIAL,
        }

    @app.post("/fixture/stage")
    def stage(request: Request, value: dict):
        nonlocal generation
        authenticate(request)
        phase = value["phase"]
        if phase not in deployments or phase == "cold":
            raise HTTPException(422, "fixture_phase_unknown")
        with state_lock:
            if phase in operations:
                return operations[phase]
            if gate.status()["expires_at"] < clock.utc() + 90:
                gate.close()
                verifier.certificate = _certificate(expires_in=300)
                generation = gate.open(expected_revision=gate.status()["revision"]).generation
            with registry.db.transaction() as conn:
                principal, link = current_in(conn)
                if conn.execute(
                    "SELECT 1 FROM bindings WHERE player_id=%s", (link.player_id,)
                ).fetchone():
                    raise HTTPException(409, "fixture_requires_unbound_player")
                cohort = current_cohort_in(conn, DEVICE_ID, 1, clock.utc())
                # Explicit synthetic rollback acceptance, built only around real current evidence.
                qualification = uuid4()
                conn.execute(
                    "INSERT INTO node_app_qualifications VALUES(%s,%s,1,%s,%s,%s)",
                    (
                        qualification,
                        DEVICE_ID,
                        link.environment_sha256,
                        "fixture:rollback-acceptance-not-qualified",
                        clock.utc(),
                    ),
                )
                conn.execute(
                    "INSERT INTO node_environment_acceptances VALUES(%s,%s,%s,1,%s,%s,%s,%s,%s)",
                    (
                        uuid4(),
                        qualification,
                        DEVICE_ID,
                        base.content_key,
                        link.environment_sha256,
                        Jsonb(cohort),
                        Jsonb({"fixture": True, "not_release_qualification": True}),
                        clock.utc(),
                    ),
                )
                before = {
                    "process": asdict(link.process),
                    "app_epoch": link.app_epoch,
                    "authority_epoch": link.authority_epoch,
                    "environment_sha256": link.environment_sha256,
                }
            operation = uuid4()
            result = lifecycle.stage(
                DEVICE_ID,
                OperatorAppStage(
                    operation,
                    uuid4(),
                    principal.grant.session_id,
                    1,
                    deployments[phase].deployment_id,
                    generation,
                    "fixture:pid1-stage-" + phase,
                ),
            )
            operations[phase] = {"operation_id": str(operation), "before": before, **result}
            return operations[phase]

    def effect_diagnostics(conn, operation):
        """Read exact owner predicates and reported effects; never ACK or report here."""
        result = {}
        try:
            row = conn.execute(
                "SELECT * FROM node_app_operations WHERE operation_id=%s", (operation,)
            ).fetchone()
            result["operation_present"] = row is not None
            if row is None:
                return result
            command = parse_stage_command(bytes(row["command_payload"]))
            current = conn.execute(
                "SELECT session_id FROM node_sessions WHERE device_id=%s "
                "AND device_generation=%s AND owner='app_effect_broker' AND revoked_at IS NULL",
                (command.producer.device_id, command.producer.device_generation),
            ).fetchone()
            result["current_session_present"] = current is not None
            if current is None:
                return result
            principal = sessions.load_operator_target_in(conn, current["session_id"])
            result["producer_matches_command"] = principal.grant.producer == command.producer
            result["session_matches_command"] = (
                principal.grant.session_id == command.command_session_id
            )
            result["command_authority_epoch"] = row["authority_epoch"]
            eligible, reason = command_eligibility_in(conn, principal.grant.offer_id)
            result["boot_eligible"] = eligible
            if not eligible:
                result["boot_refusal"] = reason
            effects = conn.execute(
                "SELECT payload FROM node_app_effects WHERE operation_id=%s ORDER BY sequence",
                (operation,),
            ).fetchall()
            events = [parse_app_effect_event(bytes(e["payload"])) for e in effects]
            event = events[-1] if events else None
            result["terminal_effect"] = event is not None and event.phase in (
                "running",
                "fallback_running",
            )
            cuts = {
                phase: min((e.sequence for e in events if e.phase == phase), default=0)
                for phase in ("intent_stop", "stopped")
            }
            result["ordered_stop_before_terminal"] = bool(
                event and 0 < cuts["intent_stop"] < cuts["stopped"] < event.sequence
            )
            link = load_current_node_app_link_in(conn, principal)
            result["current_link_present"] = link is not None
            if link is None:
                return result
            result.update(
                link_authority_epoch=link.authority_epoch,
                authority_advanced=link.authority_epoch > row["authority_epoch"],
                process_matches_event=bool(event and link.process == event.process),
                app_epoch_matches_event=bool(event and link.app_epoch == event.app_epoch),
                environment_matches_event=bool(
                    event and link.environment_sha256 == event.environment_sha256
                ),
                unbound=not bool(
                    conn.execute(
                        "SELECT 1 FROM bindings WHERE player_id=%s", (link.player_id,)
                    ).fetchone()
                ),
            )
            control = load_current_app_control_in(conn, command.producer.device_id)
            result["current_control_present"] = control is not None
            if control is None:
                return result
            receipt = ControlAppliedReceipt.model_validate_json(link.control_receipt)
            result["receipt_matches_current_control"] = current_control_receipt_matches(
                receipt, control
            )
            result["control"] = dict(
                status=control.status,
                schema_version=control.schema_version,
                issued_sequence=control.issued_sequence,
                applied_sequence=control.applied_sequence,
                receipt_sequence=receipt.delivery_sequence,
                last_result_sequence=control.last_result_sequence,
                negotiated=control.status == "negotiated",
                schema_v2=control.schema_version == 2,
                applied_at_present=control.applied_at is not None,
                last_result_at_present=control.last_result_at is not None,
                last_result_applied=control.last_result == "applied",
                issued_equals_applied=control.issued_sequence == control.applied_sequence,
                no_pending_id=control.pending_id is None,
                no_pending_digest=control.pending_digest is None,
                no_pending_expiry=control.pending_expires is None,
                last_result_sequence_matches=control.last_result_sequence
                == control.applied_sequence,
                last_delivery_matches=control.last_delivery_id == control.applied_delivery_id,
                last_digest_matches=control.last_result_digest == control.applied_digest,
                receipt_authority_matches=receipt.authority_epoch == control.authority_epoch,
                receipt_delivery_matches=receipt.delivery_id == control.applied_delivery_id,
                receipt_sequence_matches=receipt.delivery_sequence == control.applied_sequence,
                receipt_digest_matches=receipt.state_digest == control.applied_digest,
                receipt_nonce_matches=receipt.ack_nonce == control.applied_ack_nonce,
            )
        except NodeControlError as error:
            result["owner_refusal"] = error.code
        except (ValueError, TypeError, KeyError) as error:
            result["diagnostic_error_type"] = type(error).__name__
        return result

    @app.get("/fixture/status")
    def status(request: Request):
        authenticate(request)
        # Central's own projection, read before this diagnostic cut takes its locks.
        projected = {item["operation_id"]: item["state"]
                     for item in lifecycle.status(DEVICE_ID)["operations"]}
        with registry.db.transaction() as conn:
            acquire_runtime_locks(conn)
            try:
                _, link = current_in(conn)
                current = {
                    "process": asdict(link.process),
                    "app_epoch": link.app_epoch,
                    "authority_epoch": link.authority_epoch,
                    "environment_sha256": link.environment_sha256,
                }
            except NodeControlError:
                current = None
            rows = []
            for phase, entry in operations.items():
                operation = entry["operation_id"]
                effects = conn.execute(
                    "SELECT phase,sequence,payload FROM node_app_effects "
                    "WHERE operation_id=%s ORDER BY sequence",
                    (operation,),
                ).fetchall()
                rows.append(
                    {
                        "phase": phase,
                        "operation_id": operation,
                        "before": entry["before"],
                        "effects": [
                            {"phase": e["phase"], "sequence": e["sequence"]} for e in effects
                        ],
                        "state": projected.get(operation),
                        "effect_diagnostics": effect_diagnostics(conn, operation),
                    }
                )
            # Each kernel boot's admission and its unrevoked sessions, oldest first.
            admissions = conn.execute(
                "SELECT b.kernel_boot_id, b.superseded_at IS NOT NULL AS superseded, "
                "count(s.session_id) FILTER (WHERE s.revoked_at IS NULL) AS live_sessions "
                "FROM node_boot_admissions b LEFT JOIN node_producers p USING(admission_id) "
                "LEFT JOIN node_sessions s USING(producer_id) WHERE b.device_id=%s "
                "GROUP BY b.admission_id ORDER BY b.admitted_at",
                (DEVICE_ID,),
            ).fetchall()
            return {
                "current": current,
                "operations": rows,
                "admissions": [
                    {
                        "kernel_boot_id": str(a["kernel_boot_id"]),
                        "superseded": a["superseded"],
                        "live_sessions": a["live_sessions"],
                    }
                    for a in admissions
                ],
                "response_losses": dict(dropped),
                "outage": {
                    "started": outage["started"] is not None,
                    "active": outage_active(clock.utc()),
                    "dropped_seconds": (outage["last_drop"] - outage["started"])
                    if outage["last_drop"] is not None else 0,
                },
                "protocol_refusals": dict(protocol_refusals),
                "fixture_qualification": True,
            }

    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            if not thread.is_alive():
                raise RuntimeError("fixture_central_start_failed")
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("fixture_central_start_timeout")
        yield {
            "origin": origin,
            "host_origin": host_origin,
            "fixture_token": token,
            "app": app,
            "registry": registry,
        }
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        listener.close()
        # The served archive copies are the fixture's own (several GB); evidence stays.
        shutil.rmtree(workdir / "cache", ignore_errors=True)
        if thread.is_alive():
            raise RuntimeError("fixture_central_stop_timeout")


def assert_phase_completed(status, phase, target_reference):
    """Assert reported-effect and actual-link consequences; never infer physical completion."""
    entries = [entry for entry in status["operations"] if entry["phase"] == phase]
    assert len(entries) == 1, status
    entry = entries[0]
    before, current = entry["before"], status["current"]
    assert current is not None, status
    phases = [effect["phase"] for effect in entry["effects"]]
    state = "fallback_running" if phase == "failure" else "target_running"
    assert entry["state"] == state, status
    expected = (
        before["environment_sha256"]
        if phase == "failure"
        else target_reference.environment_sha256
    )
    assert current["environment_sha256"] == expected, status
    assert current["process"] != before["process"], status
    assert current["app_epoch"] > before["app_epoch"], status
    assert current["authority_epoch"] > before["authority_epoch"], status
    order = (
        ["intent_stop", "stopped", "target_failed", "fallback_starting", "fallback_running"]
        if phase == "failure"
        else ["intent_stop", "stopped", "starting_new", "running"]
    )
    positions = [phases.index(name) for name in order]
    assert all(left < right for left, right in zip(positions, positions[1:])), status
    assert "effect_unknown" not in phases, status
    # The reported terminal effect is the exact current process, after an ordered stop.
    diagnostics = entry["effect_diagnostics"]
    for predicate in ("terminal_effect", "ordered_stop_before_terminal", "process_matches_event",
                      "app_epoch_matches_event", "environment_matches_event",
                      "authority_advanced", "boot_eligible"):
        assert diagnostics.get(predicate) is True, (predicate, status)
    # Nothing held: every effect report was accepted and no control delivery is pending.
    assert status["protocol_refusals"] == {}, status
    control = diagnostics.get("control", {})
    for predicate in ("issued_equals_applied", "no_pending_id", "no_pending_digest",
                      "no_pending_expiry", "last_result_applied"):
        assert control.get(predicate) is True, (predicate, status)
    if phase == "outage":
        assert status["response_losses"]["outage"] > 0, status
        assert not status["outage"]["active"], status
        assert status["outage"]["dropped_seconds"] >= OUTAGE_SECONDS - 5, status
    return {
        "phase": phase,
        "state": entry["state"],
        "operation_id": entry["operation_id"],
        "physical_output": "unqualified",
        "rollback_acceptance": "explicit_fixture_not_qualification",
    }
