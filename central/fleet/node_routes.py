"""Mounted V2 node HTTP composition, and the Releases page's routes (central/content_catalog/views.py's
table: a version per Pi, Q2 A, 2026-10-16). Install is `PlayerVersions.install`, the same call Central's
hub method installPlayer makes (central/hub_api.py)."""
from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from functools import partial
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Path, Request
from fastapi.responses import JSONResponse, Response
from pydantic import TypeAdapter, ValidationError

from central.actor import Actor
from central.content_catalog.ports import VersionRefused
from central.content_catalog.views import ReleasesView, VersionChoice
from central.content_routes import (
    ClientDisconnected,
    error_response,
    stream_opened,
    until_disconnect,
)
from central.content_wiring import ContentServices
from central.db import Database, TransactionClock
from central.fleet.bytes import OfferByteReader
from central.fleet.models import FleetError, TrialRefusal, TrialRefused
from central.fleet.node_app_links import NodeAppLinks
from central.fleet.node_boot import NodeBootService
from central.fleet.node_calibration import NodeCalibration, TrialBegin, TrialOperation
from central.fleet.node_observations import NodeObservations
from central.fleet.node_release_catalog import NodeReleaseCatalog
from central.fleet.node_sessions import NodeControlConfig, NodeControlError, NodeSessions
from central.fleet.pi_reboot import OperatorReboots, parse_operator_reboot
from central.fleet.position_views import PositionRestore
from central.fleet.principal import PrincipalError
from central.frame_lighting.model import FrameLightingRefused
from central.frame_lighting.paper_profiles import LUT_RECIPE_ID
from central.infra.frame_lighting_store import PgFrameLighting
from central.infra.player_versions import PgPlayerVersions
from central.registry import Registry
from central.request_body import capped_body
from contracts.central_api import InstallRefusal, InstallStarted
from contracts.node_app_link import MAX_NODE_LINK_BYTES
from contracts.node_boot import MAX_NODE_BOOT_BYTES, encode_node_boot_offer, parse_node_boot_request
from contracts.node_commands import MAX_COMMAND_BYTES, encode_session_grant, parse_session_claim
from contracts.node_link import SERIAL_PATTERN
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


# The Releases routes' refusals: one status per InstallRefusal (central/content_catalog/views.py).
VERSION_REFUSAL_STATUS: dict[InstallRefusal, int] = {
    InstallRefusal.UNKNOWN_PLAYER: 404, InstallRefusal.UNKNOWN_VERSION: 404, InstallRefusal.NOT_DEPLOYABLE: 409}
SerialPath = Annotated[str, Path(pattern=rf"^{SERIAL_PATTERN}$")]

_BEGIN: TypeAdapter[TrialBegin] = TypeAdapter(TrialBegin)
_OPERATION: TypeAdapter[TrialOperation] = TypeAdapter(TrialOperation)


MAX_REBOOT_BYTES = 1024   # the operator's reboot body: a command id and a one-word reason


def _trial_body[T](adapter: TypeAdapter[T], raw: bytes) -> T:
    """A trial body, strict JSON (contracts.strict_json's rules, then pydantic's strict validation);
    TrialRefused(TrialRefusal.OPERATION_INVALID) for anything else."""
    if loads_object(raw, max_bytes=4096) is None:
        raise TrialRefused(TrialRefusal.OPERATION_INVALID)
    try:
        return adapter.validate_json(raw, strict=True)
    except ValidationError as exc:
        raise TrialRefused(TrialRefusal.OPERATION_INVALID) from exc


def mount_node_routes(app: FastAPI, *, db: Database, clock: Clock,
                      admin: Callable[..., None], config: NodeControlConfig,
                      times: TransactionClock, content: ContentServices | None = None) -> None:
    sessions = NodeSessions(db, clock, config)
    observations = NodeObservations(sessions)
    bytes_reader = OfferByteReader(content.reader if content else None)
    reboots = OperatorReboots(db, content.publisher if content else None)
    # Each Pi's version needs the cache's readiness: without content services (a fake database) no
    # release is ready, so no boot is offered and the Releases routes are not mounted.
    readiness = content.readiness if content else None
    versions = (None if readiness is None
                else PgPlayerVersions(db, clock, readiness, content.publisher if content else None))
    boots = None if versions is None else NodeBootService(sessions, versions=versions)
    links = NodeAppLinks(sessions)
    publications = None if readiness is None else NodeReleaseCatalog(sessions, readiness=readiness)
    lighting = PgFrameLighting(LUT_RECIPE_ID)
    # A trial's times are the database's (the document source's clock) in production; a test that
    # injects its own clock passes its times (create_app's media_times).
    trials = NodeCalibration(db, registry=Registry(db, clock, lighting=lighting), lighting=lighting, times=times)
    app.state.node_app_links = links
    app.state.node_sessions = sessions
    app.state.player_versions = versions

    @app.middleware("http")
    async def node_no_store(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/v2/node/"):  # operator routes: operator_auth
            response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.exception_handler(FleetError)
    async def fleet_error(_request: Request, exc: FleetError) -> JSONResponse:
        """Each refusal carries its own Retry-After (the device fence's, the offer bytes')."""
        return error_response(exc.code, exc.status, retry_after=exc.retry_after)

    @app.exception_handler(NodeControlError)
    async def node_error(_request: Request, error: NodeControlError):
        return JSONResponse({"error": error.code, **error.details}, status_code=error.status)

    @app.exception_handler(VersionRefused)
    async def version_refused(_request: Request, error: VersionRefused):
        return JSONResponse({"error": error.refusal.value}, status_code=VERSION_REFUSAL_STATUS[error.refusal])

    @app.exception_handler(FrameLightingRefused)
    async def lighting_refused(_request: Request, error: FrameLightingRefused):
        return JSONResponse({"error": error.code.value}, status_code=error.status)

    async def invoke(function, *args):
        try:
            return await asyncio.to_thread(function, *args)
        except PrincipalError as exc:
            raise NodeControlError(str(exc), 503) from exc
        except NodeControlError:
            raise
        except ValueError as exc:
            raise NodeControlError(str(exc), 422) from exc

    async def body(request: Request, limit: int) -> bytes:
        """Bound streamed content even when Content-Length is absent or dishonest."""
        return await capped_body(request, limit, lambda: NodeControlError("node_body_too_large", 413))

    @app.post("/v2/node/boot-offers")
    async def boot_offer(request: Request):
        raw = await body(request, MAX_NODE_BOOT_BYTES)
        try:
            parsed = parse_node_boot_request(raw)
        except ValueError as exc:
            raise NodeControlError("invalid_node_boot_request", 422) from exc
        if boots is None:   # no cache: no release is ready
            raise NodeControlError("node_boot_policy_unconfigured", 503)
        offer = await invoke(boots.offer, parsed)
        return Response(encode_node_boot_offer(offer), media_type="application/json")

    @app.get("/v2/node/boot-offers/{offer_id}/artifacts/{role}")
    async def boot_artifact(request: Request, offer_id: UUID, role: str):
        if boots is None:
            raise NodeControlError("node_boot_offer_unknown", 404)
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

    if versions is not None and publications is not None:
        _mount_releases(app, admin, versions, publications)

    @app.post("/v2/node/sessions")
    async def enroll(request: Request):
        raw = await body(request, MAX_COMMAND_BYTES)
        try:
            claim = parse_session_claim(raw)
        except ValueError as exc:
            raise NodeControlError("invalid_node_session_claim", 422) from exc
        grant = await invoke(sessions.enroll, claim)
        return Response(encode_session_grant(grant), media_type="application/json")

    @app.post("/v2/node/app-links")
    async def app_link(request: Request):
        raw = await body(request, MAX_NODE_LINK_BYTES)
        return await invoke(links.admit, *_credentials(request), raw)

    @app.get("/v1/operator/frames/{frame_id}/calibration-capability", dependencies=[Depends(admin)])
    async def calibration_capability(frame_id: str):
        return await invoke(Registry(db, clock, lighting=lighting).calibration_capability, frame_id)

    @app.post("/v1/operator/frames/{frame_id}/calibration-trials")
    async def calibration_begin(frame_id: str, request: Request, by: Actor = Depends(admin)):
        # An empty body, or `{}`, begins at the saved calibration and lighting (TrialBegin).
        raw = await body(request, 4096)
        begin = _trial_body(_BEGIN, raw if raw.strip() else b"{}")
        return await invoke(lambda: trials.begin(frame_id, begin, by=by))

    @app.post("/v1/operator/frames/{frame_id}/calibration-trials/{trial_id}")
    async def calibration_operate(frame_id: str, trial_id: UUID, request: Request, by: Actor = Depends(admin)):
        operation = _trial_body(_OPERATION, await body(request, 4096))
        return await invoke(lambda: trials.operate(frame_id, trial_id, operation, by=by))

    @app.post("/v1/operator/frames/{frame_id}/calibration-trials/{trial_id}/restores")
    async def calibration_restore(frame_id: str, trial_id: UUID, request: Request, by: Actor = Depends(admin)):
        # Configuration snapshots' Restore: a Position history entry into the trial's draft; the
        # answer is the trial's view, as an edit's (the console replaces its draft with it).
        value = loads_object(await body(request, 4096), max_bytes=4096)
        try:
            restore = PositionRestore.model_validate(value)
        except ValidationError as exc:
            raise TrialRefused(TrialRefusal.OPERATION_INVALID) from exc
        return await invoke(lambda: trials.restore(frame_id, trial_id, entry_id=restore.entry_id,
                                                   expected_sequence=restore.expected_sequence, by=by))

    @app.post("/v1/operator/node/devices/{device_id}/reboots")
    async def reboot(device_id: str, request: Request, by: Actor = Depends(admin)):
        """The operator's reboot over the bus (S4a's `RebootPi`): {"command_id", "reason"}."""
        raw = await body(request, MAX_REBOOT_BYTES)
        try:
            command = parse_operator_reboot(loads_object(raw, max_bytes=MAX_REBOOT_BYTES))
        except ValueError as exc:
            raise NodeControlError("invalid_operator_reboot", 422) from exc
        return await invoke(lambda: reboots.request_operator_reboot(device_id, command, by=by))

    @app.get("/v1/operator/node/hosts", dependencies=[Depends(admin)])
    async def fleet_hosts():
        return await invoke(observations.fleet_hosts)

    @app.get("/v1/operator/node/devices/{device_id}", dependencies=[Depends(admin)])
    async def status(device_id: str):
        return await invoke(observations.status, device_id)


def _mount_releases(app: FastAPI, admin: Callable[..., None], versions: PgPlayerVersions,
                    publications: NodeReleaseCatalog) -> None:
    """The Releases page's three routes (central/content_catalog/views.py), statuses as there."""

    @app.get("/v1/operator/releases", dependencies=[Depends(admin)], response_model=ReleasesView)
    async def releases() -> ReleasesView:
        return await asyncio.to_thread(publications.view)

    @app.put("/v1/operator/releases/wall", response_model=ReleasesView)
    async def update_the_wall(choice: VersionChoice, by: Actor = Depends(admin)) -> ReleasesView:
        await asyncio.to_thread(partial(versions.set_wall_version, choice.version, by=by))
        return await asyncio.to_thread(publications.view)

    @app.post("/v1/operator/players/{serial}/install", response_model=InstallStarted)
    async def install(serial: SerialPath, choice: VersionChoice, by: Actor = Depends(admin)) -> InstallStarted:
        return await asyncio.to_thread(partial(versions.install, serial, choice.version, by=by))
