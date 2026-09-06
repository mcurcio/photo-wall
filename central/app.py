"""Central operator and enrollment HTTP adapter. Domain authority lives in Registry."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field, model_validator

from central.coordination import CoordinationLimits, Coordinator
from central.db import Database
from central.media_gateway import MediaGateway
from central.media_store import MediaStore
from central.registry import Enrollment, FrameCreate, Registry, RegistryError
from central.runtime import Program, Scene
from contracts.models import Calibration, Identifier, Model, Observation, Readiness
from contracts.time import Clock, SystemClock
from media.models import SourceSpec


class Challenge(Model):
    public_key: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class BindingRequest(Model):
    player_id: Identifier
    output_id: Identifier
    expected_generation: int = Field(ge=0)


class CalibrationRequest(Model):
    operation: Literal["preview", "commit", "revert"]
    expected_revision: int = Field(ge=1)
    expected_generation: int = Field(ge=1)
    calibration: Calibration | None = None


class ActivationRequest(Model):
    scene_id: Identifier
    activation_id: Identifier
    priority: int = 0
    repeat: Literal["ignore", "restart", "queue"] = "ignore"
    force: bool = False
    expires_at: float | None = None


class AuthoredCandidatesRequest(Model):
    source_ref: Identifier
    asset_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_assets(self):
        if len(self.asset_ids) != len(set(self.asset_ids)):
            raise ValueError("asset_ids must be unique")
        return self


def create_app(db: Database | None = None, clock: Clock | None = None,
               admin_token: str | None = None, *, run_scheduler: bool | None = None,
               media_root: Path | None = None) -> FastAPI:
    run_scheduler = clock is None if run_scheduler is None else run_scheduler
    db = db or Database(os.environ["PHOTO_WALL_DATABASE_URL"])
    clock = clock or SystemClock()
    admin_token = admin_token or os.environ["PHOTO_WALL_ADMIN_TOKEN"]
    if len(admin_token) < 32:
        raise ValueError("PHOTO_WALL_ADMIN_TOKEN must contain at least 32 characters")
    registry = Registry(db, clock)
    coordinator = Coordinator(db, clock, CoordinationLimits(
        horizon_seconds=float(os.environ.get("PHOTO_WALL_HORIZON_SECONDS", "300"))))
    media_root = media_root or (Path(os.environ["PHOTO_WALL_MEDIA_ROOT"])
                               if "PHOTO_WALL_MEDIA_ROOT" in os.environ else None)
    media_gateway = MediaGateway(MediaStore(coordinator.media, media_root)) if media_root else None
    scheduler_health = {"running": False, "last_tick": None, "error": None}

    async def scheduler():
        scheduler_health["running"] = True
        try:
            while True:
                try:
                    projection = await asyncio.to_thread(coordinator.advance)
                    await asyncio.to_thread(coordinator.media.request_acquisitions, projection.acquisitions)
                    scheduler_health.update(last_tick=clock.utc(), error=None)
                except Exception:
                    # Sanitized health only: driver exceptions may include connection secrets.
                    scheduler_health["error"] = "coordination_unavailable"
                await asyncio.sleep(1)
        finally:
            scheduler_health["running"] = False

    @asynccontextmanager
    async def lifespan(app):
        db.migrate()
        task = asyncio.create_task(scheduler()) if run_scheduler else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(title="Photo Wall", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.registry = registry
    app.state.coordinator = coordinator
    bearer = HTTPBearer(auto_error=False)

    def admin(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if not credentials or not secrets.compare_digest(credentials.credentials, admin_token):
            raise RegistryError("unauthorized", 401)

    def player(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if not credentials:
            raise RegistryError("unauthorized", 401)
        return registry.authenticate(credentials.credentials)

    @app.exception_handler(RegistryError)
    async def registry_error(request, exc):
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Do not reflect request bodies, private keys, token strings, or upstream fields.
        return JSONResponse({"error": "invalid_request"}, status_code=422)

    @app.exception_handler(ValueError)
    async def invalid_command(request, exc):
        return JSONResponse({"error": "invalid_command"}, status_code=422)

    @app.get("/healthz")
    def health():
        try:
            healthy = db.healthy()
        except Exception:
            healthy = False
        return JSONResponse({"status": "ok" if healthy else "unavailable", "database": healthy,
                             "protocol": 1, "scheduler": scheduler_health}, status_code=200 if healthy else 503)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(Path(__file__).with_name("operator.html"),
                            headers={"Cache-Control": "no-store",
                                     "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"})

    @app.get("/operator.js", include_in_schema=False)
    def operator_script():
        return FileResponse(Path(__file__).with_name("operator.js"), media_type="text/javascript",
                            headers={"Cache-Control": "no-store"})

    @app.post("/v1/enrollment/challenge")
    def challenge(request: Challenge):
        return registry.challenge(request.public_key)

    @app.post("/v1/enrollment/register")
    def register(request: Enrollment):
        return registry.enroll(request)

    @app.get("/v1/player/config")
    def player_config(identity: dict = Depends(player)):
        config = coordinator.configuration(identity["id"], identity["authority_epoch"])
        return {**config.model_dump(mode="json"), "server_time": clock.utc()}

    @app.get("/v1/player/state")
    def player_state(identity: dict = Depends(player)):
        return coordinator.delivery(identity["id"], identity["authority_epoch"])

    @app.get("/v1/media/{digest}")
    def media_file(digest: str, request: Request,
                   credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if credentials is None:
            raise RegistryError("unauthorized", 401)
        if "range" in request.headers:
            raise RegistryError("media_range_unsupported", 416)
        if media_gateway is None:
            raise RegistryError("media_unavailable", 503)
        return media_gateway.response(credentials.credentials, digest)

    @app.post("/v1/player/readiness")
    def readiness(request: Readiness, identity: dict = Depends(player)):
        if request.authority_epoch != identity["authority_epoch"]:
            raise RegistryError("stale_authority", 403)
        return {"accepted": coordinator.readiness(identity["id"], request)}

    @app.post("/v1/player/observations")
    def observation(request: Observation, identity: dict = Depends(player)):
        if request.authority_epoch != identity["authority_epoch"]:
            raise RegistryError("stale_authority", 403)
        coordinator.observe(identity["id"], request)
        return {"accepted": True}

    @app.websocket("/v1/player/session")
    async def session(websocket: WebSocket):
        authorization = websocket.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            await websocket.close(code=1008)
            return
        token = authorization.removeprefix("Bearer ")
        try:
            identity = await asyncio.to_thread(registry.authenticate, token)
        except RegistryError:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            while True:
                # Reauthenticate existing sessions too, including token rotation/retirement.
                current = await asyncio.to_thread(registry.authenticate, token)
                if current != identity:
                    raise RegistryError("stale_authority", 403)
                state = await asyncio.to_thread(coordinator.delivery, identity["id"], identity["authority_epoch"])
                await websocket.send_json({"type": "state", "configuration": state["configuration"].model_dump(mode="json"),
                    "plan": state["plan"].model_dump(mode="json") if state["plan"] else None,
                    "commits": [c.model_dump(mode="json") for c in state["commits"]],
                    "revocations": [r.model_dump(mode="json") for r in state["revocations"]], "server_time": state["server_time"]})
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(), timeout=.5)
                except asyncio.TimeoutError:
                    continue
                if len(raw.encode()) > 1024**2:
                    raise ValueError("session message limit")
                message = json.loads(raw)
                if not isinstance(message, dict) or set(message) != {"type", "payload"}:
                    raise ValueError("invalid session message")
                if message["type"] == "readiness":
                    report = Readiness.model_validate(message["payload"])
                    if report.authority_epoch != identity["authority_epoch"]:
                        raise RegistryError("stale_authority", 403)
                    await asyncio.to_thread(coordinator.readiness, identity["id"], report)
                elif message["type"] == "observation":
                    observed = Observation.model_validate(message["payload"])
                    if observed.authority_epoch != identity["authority_epoch"]:
                        raise RegistryError("stale_authority", 403)
                    await asyncio.to_thread(coordinator.observe, identity["id"], observed)
                else:
                    raise ValueError("invalid session message")
        except WebSocketDisconnect:
            pass
        except (RegistryError, ValueError):
            await websocket.close(code=1008)

    @app.get("/v1/operator/inventory", dependencies=[Depends(admin)])
    def inventory():
        return registry.inventory()

    @app.post("/v1/operator/frames", dependencies=[Depends(admin)], status_code=201)
    def create_frame(frame: FrameCreate):
        return registry.create_frame(frame)

    @app.put("/v1/operator/frames/{frame_id}/binding", dependencies=[Depends(admin)])
    def bind(frame_id: Identifier, binding: BindingRequest):
        return registry.bind(frame_id, binding.player_id, binding.output_id,
                             expected_generation=binding.expected_generation)

    @app.post("/v1/operator/frames/{frame_id}/calibration", dependencies=[Depends(admin)])
    def calibrate(frame_id: Identifier, request: CalibrationRequest):
        return registry.calibrate(frame_id, request.operation, request.expected_revision,
                                  request.calibration, expected_generation=request.expected_generation)

    @app.post("/v1/operator/players/{player_id}/retire", dependencies=[Depends(admin)])
    def retire(player_id: Identifier):
        registry.retire(player_id)
        return {"status": "retired"}

    @app.get("/v1/operator/runtime", dependencies=[Depends(admin)])
    def runtime_state():
        runtime = coordinator.runtime.read()
        return {"definitions": runtime.export_state()["scenes"],
                "programs": runtime.export_state()["programs"], "current": runtime.project(clock.utc())}

    @app.get("/v1/operator/media", dependencies=[Depends(admin)])
    def media_state():
        return {"sources": coordinator.media.sources(), "health": coordinator.media.health()}

    @app.put("/v1/operator/sources/{source_ref}", dependencies=[Depends(admin)])
    def configure_source(source_ref: Identifier, source: SourceSpec):
        if source.source_ref != source_ref:
            raise ValueError("Source identity mismatch")
        return {"created": coordinator.media.configure_source(source)}

    @app.get("/v1/operator/sources/{source_ref}/candidates", dependencies=[Depends(admin)])
    def source_candidates(source_ref: Identifier):
        return coordinator.media.source_candidates(source_ref)

    @app.post("/v1/operator/authored-candidates", dependencies=[Depends(admin)])
    def author_candidates(request: AuthoredCandidatesRequest):
        return coordinator.media.author_authored_candidates(request.source_ref, request.asset_ids)

    @app.put("/v1/operator/scenes/{scene_id}", dependencies=[Depends(admin)])
    def configure_scene(scene_id: Identifier, scene: Scene):
        if scene.scene_id != scene_id:
            raise ValueError("Scene identity mismatch")
        coordinator.runtime.command("set_scene", scene)
        return {"status": "configured"}

    @app.put("/v1/operator/programs/{program_id}", dependencies=[Depends(admin)])
    def configure_program(program_id: Identifier, program: Program):
        if program.program_id != program_id:
            raise ValueError("Program identity mismatch")
        coordinator.runtime.command("set_program", program)
        return {"status": "configured"}

    @app.delete("/v1/operator/programs/{program_id}", dependencies=[Depends(admin)])
    def remove_program(program_id: Identifier):
        return coordinator.runtime.command("remove_program", program_id, clock.utc())

    @app.post("/v1/operator/activations", dependencies=[Depends(admin)])
    def activate(request: ActivationRequest):
        return coordinator.runtime.command("activate", request.scene_id, request.activation_id, clock.utc(),
            priority=request.priority, repeat=request.repeat, force=request.force, expires_at=request.expires_at)

    @app.post("/v1/operator/runs/{run_id}/{operation}", dependencies=[Depends(admin)])
    def control_run(run_id: Identifier, operation: Literal["finish", "cancel"]):
        return coordinator.runtime.command(operation, run_id, clock.utc())

    return app
