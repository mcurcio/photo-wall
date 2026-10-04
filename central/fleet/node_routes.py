"""Mounted V2 node HTTP composition; effect rollout admission remains default closed."""
from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from central.content_routes import ClientDisconnected, stream_opened, until_disconnect
from central.content_wiring import ContentServices
from central.coordination import Coordinator
from central.db import Database
from central.fleet.bytes import OfferByteReader
from central.fleet.node_acceptance import NodeAcceptance
from central.fleet.node_app_links import NodeAppLinks
from central.fleet.node_boot import NodeBootService
from central.fleet.node_calibration import NodeCalibration
from central.fleet.node_commands import NodeCommands, OperatorReboot
from central.fleet.node_display import NodeDisplay
from central.fleet.node_ingest import NodeIngest
from central.fleet.node_lifecycle import NodeLifecycle, OperatorAppStage
from central.fleet.node_observations import NodeObservations
from central.fleet.node_release_catalog import NodeReleaseCatalog
from central.fleet.node_sessions import NodeControlConfig, NodeControlError, NodeSessions
from central.fleet.principal import PrincipalError
from central.fleet.rollout_gate import RolloutEffectGate, RolloutGateError, ServingImageVerifier
from central.registry import Registry
from contracts.node_app_link import MAX_NODE_LINK_BYTES
from contracts.node_boot import MAX_NODE_BOOT_BYTES, encode_node_boot_offer, parse_node_boot_request
from contracts.node_commands import MAX_COMMAND_BYTES, encode_session_grant, parse_session_claim
from contracts.node_display import MAX_DISPLAY_BYTES
from contracts.node_host_facts import MAX_HOST_FACTS_BYTES
from contracts.node_lifecycle import MAX_LIFECYCLE_BYTES
from contracts.node_observation import MAX_OBSERVATION_BYTES
from contracts.node_preparation import MAX_PREPARATION_BYTES
from contracts.node_protocol import MAX_NODE_MESSAGE_BYTES
from contracts.strict_json import loads_object
from contracts.time import Clock


def _credentials(request: Request) -> tuple[UUID, str]:
    try:
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise ValueError("missing_bearer")
        return UUID(request.headers["x-node-session"]), authorization.removeprefix("Bearer ")
    except (KeyError, ValueError) as exc:
        raise NodeControlError("node_credential_required", 401) from exc


def _operator_reboot(raw: bytes) -> OperatorReboot:
    value = loads_object(raw, max_bytes=MAX_COMMAND_BYTES)
    if value is None:
        raise ValueError("invalid_operator_reboot")
    try:
        return OperatorReboot(**{**value, "command_id": UUID(value["command_id"]),
                                 "session_id": UUID(value["session_id"])})
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError("invalid_operator_reboot") from exc


def mount_node_routes(app: FastAPI, *, db: Database, clock: Clock,
                      admin: Callable[..., None], coordinator: Coordinator, config: NodeControlConfig | None = None,
                      serving_verifier: ServingImageVerifier | None = None,
                      content: ContentServices | None = None) -> None:
    sessions = NodeSessions(db, clock, config)
    ingest, observations = NodeIngest(sessions), NodeObservations(sessions)
    display = NodeDisplay(sessions, runtime=coordinator)
    boots = NodeBootService(sessions, publisher=content.publisher if content else None)
    bytes_reader = OfferByteReader(content.reader if content else None)
    effect_gate = RolloutEffectGate(db, serving_verifier=serving_verifier)
    commands = NodeCommands(sessions, effect_gate)
    lifecycle = NodeLifecycle(sessions, effect_gate)
    links = NodeAppLinks(sessions)
    acceptance = NodeAcceptance(sessions)
    publications = NodeReleaseCatalog(sessions, readiness=content.readiness if content else None)
    trials = NodeCalibration(sessions, display=display, registry=Registry(db, clock))
    display.trials = trials
    app.state.node_lifecycle = lifecycle
    app.state.node_sessions = sessions

    @app.middleware("http")
    async def node_no_store(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/v2/node/"):  # operator routes: operator_auth
            response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.exception_handler(NodeControlError)
    async def node_error(_request: Request, error: NodeControlError):
        return JSONResponse({"error": error.code, **error.details}, status_code=error.status)

    async def invoke(function, *args):
        try:
            return await asyncio.to_thread(function, *args)
        except (RolloutGateError, PrincipalError) as exc:
            raise NodeControlError(str(exc), 503) from exc
        except NodeControlError:
            raise
        except ValueError as exc:
            raise NodeControlError(str(exc), 422) from exc

    async def body(request: Request, limit: int) -> bytes:
        """Bound streamed content even when Content-Length is absent or dishonest."""
        sessions.require_enabled()
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            raise NodeControlError("node_body_too_large", 413)
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > limit:
                raise NodeControlError("node_body_too_large", 413)
            data.extend(chunk)
        return bytes(data)

    @app.get("/v1/operator/node/status", dependencies=[Depends(admin)])
    async def node_status():
        return {"transport_enabled": config is not None,
                "installation_audience": config.installation_audience if config else None,
                "effect_gate": await invoke(effect_gate.status),
                "command_qualification": "requires_current_scope_and_serving_image_evidence",
                "physical_qualification": "not_established_by_server_startup"}

    @app.post("/v2/node/boot-offers")
    async def boot_offer(request: Request):
        raw = await body(request, MAX_NODE_BOOT_BYTES)
        try:
            parsed = parse_node_boot_request(raw)
        except ValueError as exc:
            raise NodeControlError("invalid_node_boot_request", 422) from exc
        offer = await invoke(boots.offer, parsed)
        return Response(encode_node_boot_offer(offer), media_type="application/json")

    @app.get("/v2/node/boot-offers/{offer_id}/artifacts/{role}")
    async def boot_artifact(request: Request, offer_id: UUID, role: str):
        asset = await invoke(boots.asset, offer_id, role)
        try:
            opened = await until_disconnect(request, bytes_reader.open_exact(asset))
        except ClientDisconnected:
            return Response(status_code=499)
        try:
            return stream_opened(opened, "application/octet-stream")
        except BaseException:
            os.close(opened.fd)
            raise

    @app.put("/v1/operator/node/boot-policy", dependencies=[Depends(admin)])
    async def select_deployment(request: Request):
        raw = await body(request, MAX_COMMAND_BYTES)
        value = loads_object(raw, max_bytes=MAX_COMMAND_BYTES)
        try:
            if value is None or set(value) != {"deployment_id", "expected_revision"}:
                raise ValueError("shape")
            deployment_id = UUID(value["deployment_id"])
            return await invoke(boots.select, deployment_id, value["expected_revision"])
        except (ValueError, TypeError) as exc:
            if isinstance(exc, NodeControlError):
                raise
            raise NodeControlError("invalid_node_boot_selection", 422) from exc

    @app.get("/v1/operator/node/releases", dependencies=[Depends(admin)])
    async def node_releases():
        return await invoke(publications.list)

    @app.post("/v2/node/sessions")
    async def enroll(request: Request):
        raw = await body(request, MAX_COMMAND_BYTES)
        try:
            claim = parse_session_claim(raw)
        except ValueError as exc:
            raise NodeControlError("invalid_node_session_claim", 422) from exc
        grant = await invoke(sessions.enroll, claim)
        return Response(encode_session_grant(grant), media_type="application/json")

    @app.post("/v2/node/evidence")
    async def evidence(request: Request):
        raw = await body(request, MAX_NODE_MESSAGE_BYTES)
        return await invoke(ingest.ingest, *_credentials(request), raw)

    @app.post("/v2/node/observations")
    async def observation(request: Request):
        raw = await body(request, MAX_OBSERVATION_BYTES)
        return await invoke(observations.record, *_credentials(request), raw)

    @app.post("/v2/node/host-facts")
    async def host_facts(request: Request):
        raw = await body(request, MAX_HOST_FACTS_BYTES)
        return await invoke(observations.record_facts, *_credentials(request), raw)

    @app.post("/v2/node/app-links")
    async def app_link(request: Request):
        raw = await body(request, MAX_NODE_LINK_BYTES)
        return await invoke(links.admit, *_credentials(request), raw)

    @app.post("/v2/node/app-preparation")
    async def manager_preparation(request: Request):
        return await invoke(observations.record_preparation, *_credentials(request),
                            await body(request, MAX_PREPARATION_BYTES))

    @app.get("/v2/node/app-desired")
    async def app_desired(request: Request):
        return await invoke(lifecycle.desired, *_credentials(request))

    @app.get("/v2/node/app-commands")
    async def app_commands(request: Request):
        return await invoke(lambda sid, credential: lifecycle.desired(sid, credential, effects=True),
                            *_credentials(request))

    @app.post("/v2/node/app-effects")
    async def app_effect(request: Request):
        return await invoke(lifecycle.effect, *_credentials(request), await body(request, MAX_LIFECYCLE_BYTES))

    @app.post("/v2/node/app-responses")
    async def app_response(request: Request):
        return await invoke(lifecycle.response, *_credentials(request), await body(request, MAX_NODE_MESSAGE_BYTES))

    @app.get("/v2/node/app-attempts/{operation_id}/artifacts/{role}")
    async def app_artifact(request: Request, operation_id: UUID, role: str):
        asset = await invoke(lifecycle.artifact, *_credentials(request), operation_id, role)
        try:
            opened = await until_disconnect(request, bytes_reader.open_exact(asset))
        except ClientDisconnected:
            return Response(status_code=499)
        try:
            return stream_opened(opened, "application/octet-stream")
        except BaseException:
            os.close(opened.fd)
            raise

    @app.post("/v1/operator/node/devices/{device_id}/app-stages", dependencies=[Depends(admin)])
    async def app_stage(device_id: str, request: Request):
        value = loads_object(await body(request, MAX_LIFECYCLE_BYTES), max_bytes=MAX_LIFECYCLE_BYTES)
        try:
            if value is None:
                raise ValueError("shape")
            for key in ("operation_id", "command_id", "session_id", "deployment_id"):
                value[key] = UUID(value[key])
            stage = OperatorAppStage(**value)
        except (KeyError, TypeError, ValueError) as exc:
            raise NodeControlError("node_app_stage_invalid", 422) from exc
        return await invoke(lifecycle.stage, device_id, stage)

    @app.post("/v1/operator/node/devices/{device_id}/app-qualifications", dependencies=[Depends(admin)])
    async def app_qualification(device_id: str, request: Request):
        value = loads_object(await body(request, MAX_LIFECYCLE_BYTES), max_bytes=MAX_LIFECYCLE_BYTES)
        try:
            if value is None or set(value) != {"qualification_id", "environment_sha256", "operator_audit_ref"}:
                raise ValueError("shape")
            qualification_id = UUID(value["qualification_id"])
        except (TypeError, ValueError) as exc:
            raise NodeControlError("node_qualification_invalid", 422) from exc
        return await invoke(acceptance.begin, device_id, qualification_id,
                            value["environment_sha256"], value["operator_audit_ref"])

    @app.post("/v1/operator/node/app-qualifications/{qualification_id}/sample", dependencies=[Depends(admin)])
    async def app_qualification_sample(qualification_id: UUID):
        return await invoke(acceptance.sample, qualification_id)

    @app.get("/v1/operator/frames/{frame_id}/calibration-capability", dependencies=[Depends(admin)])
    async def calibration_capability(frame_id: str):
        return await invoke(Registry(db, clock).calibration_capability, frame_id)

    @app.post("/v1/operator/frames/{frame_id}/calibration-trials", dependencies=[Depends(admin)])
    async def calibration_begin(frame_id: str):
        return await invoke(trials.begin, frame_id)

    @app.post("/v1/operator/frames/{frame_id}/calibration-trials/{trial_id}", dependencies=[Depends(admin)])
    async def calibration_operate(frame_id: str, trial_id: UUID, request: Request):
        value = loads_object(await body(request, 4096), max_bytes=4096)
        try:
            if value is None or set(value)-{"operation", "expected_sequence", "calibration"} or not {
                    "operation", "expected_sequence"} <= set(value):
                raise ValueError("shape")
        except (TypeError, ValueError) as exc:
            raise NodeControlError("node_trial_operation_invalid", 422) from exc
        return await invoke(lambda: trials.operate(frame_id, trial_id, **value))

    @app.post("/v2/node/display")
    async def display_exchange(request: Request):
        raw = await body(request, MAX_DISPLAY_BYTES)
        result = await invoke(display.exchange, *_credentials(request), raw)
        return Response(result, media_type="application/json")

    @app.get("/v2/node/commands")
    async def poll(request: Request):
        sessions.require_enabled()
        return await invoke(commands.poll, *_credentials(request))

    @app.post("/v1/operator/node/devices/{device_id}/reboots", dependencies=[Depends(admin)])
    async def reboot(device_id: str, request: Request):
        raw = await body(request, MAX_COMMAND_BYTES)
        try:
            command = _operator_reboot(raw)
        except ValueError as exc:
            raise NodeControlError("invalid_operator_reboot", 422) from exc
        return await invoke(commands.request_reboot, device_id, command)

    @app.get("/v1/operator/node/devices/{device_id}/app-attempts", dependencies=[Depends(admin)])
    async def app_status(device_id: str):
        return await invoke(lifecycle.status, device_id)

    @app.get("/v1/operator/node/hosts", dependencies=[Depends(admin)])
    async def fleet_hosts():
        return await invoke(observations.fleet_hosts)

    @app.get("/v1/operator/node/devices/{device_id}", dependencies=[Depends(admin)])
    async def status(device_id: str):
        return await invoke(observations.status, device_id)
