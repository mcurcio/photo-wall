"""Central operator and enrollment HTTP adapter. Domain authority lives in Registry."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import stat
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field, model_validator

from central.app_packages import AppPackageError, AppPackages
from central.coordination import CoordinationLimits, Coordinator
from central.db import Database
from central.execution_repository import PostgresExecutionRepository
from central.installation_models import InstallationInventory
from central.mdns_advertise import MdnsCentralAdvertiser
from central.media_gateway import MediaGateway
from central.media_ports import MediaApplication, RefreshReceipt, SourceConfigurationReceipt
from central.media_queue import MediaTaskQueue, ProcrastinateMediaQueue
from central.media_repository import MediaRepository
from central.media_store import MediaStore
from central.registry import Enrollment, FrameCreate, Registry, RegistryError
from central.runtime import Program, Scene
from contracts.models import (
    Calibration,
    Digest,
    Identifier,
    Model,
    Observation,
    PlayerTime,
    Readiness,
)
from contracts.time import Clock, SystemClock
from media.models import SourceSpec

SCHEDULER_MAX_AGE = 10.0


class Challenge(Model):
    public_key: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class BindingRequest(Model):
    player_id: Identifier
    output_id: Identifier
    expected_generation: int = Field(ge=0)


class UnbindRequest(Model):
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


class AuthoredSceneRequest(AuthoredCandidatesRequest):
    scene: Scene


class AppPackageRegistration(Model):
    """Records a `.deb` already staged under PHOTO_WALL_APP_ROOT by sha256."""

    version: str = Field(min_length=1, max_length=256)
    sha256: Digest
    size: int = Field(gt=0)


class AppPackagePromotion(Model):
    sha256: Digest


def create_app(
    db: Database | None = None,
    clock: Clock | None = None,
    admin_token: str | None = None,
    *,
    run_scheduler: bool | None = None,
    media_root: Path | None = None,
    media_queue: MediaTaskQueue | None = None,
    app_root: Path | None = None,
    mdns_enabled: bool | None = None,
    mdns_port: int | None = None,
    mdns_advertiser: MdnsCentralAdvertiser | None = None,
) -> FastAPI:
    run_scheduler = clock is None if run_scheduler is None else run_scheduler
    owns_db = db is None
    db = db or Database(os.environ["PHOTO_WALL_DATABASE_URL"])
    clock = clock or SystemClock()
    admin_token = admin_token or os.environ["PHOTO_WALL_ADMIN_TOKEN"]
    if len(admin_token) < 32:
        raise ValueError("PHOTO_WALL_ADMIN_TOKEN must contain at least 32 characters")
    registry = Registry(db, clock)
    media_queue = media_queue or (
        ProcrastinateMediaQueue(db.dsn) if isinstance(db, Database) else None
    )
    media_repository = MediaRepository(db, clock, queue=media_queue)
    media_application: MediaApplication = media_repository
    coordinator = Coordinator(
        db,
        clock,
        CoordinationLimits(
            horizon_seconds=float(os.environ.get("PHOTO_WALL_HORIZON_SECONDS", "300"))
        ),
        media=media_repository,
    )
    media_root = media_root or (
        Path(os.environ["PHOTO_WALL_MEDIA_ROOT"]) if "PHOTO_WALL_MEDIA_ROOT" in os.environ else None
    )
    app_root = app_root or (
        Path(os.environ["PHOTO_WALL_APP_ROOT"]) if "PHOTO_WALL_APP_ROOT" in os.environ else None
    )
    app_packages = AppPackages(db, clock)
    media_gateway = (
        MediaGateway(
            MediaStore(
                coordinator.media,
                media_root,
                installation=coordinator.installation,
                execution=PostgresExecutionRepository(),
            )
        )
        if media_root
        else None
    )
    mdns_enabled = (
        mdns_enabled
        if mdns_enabled is not None
        else os.environ.get("PHOTO_WALL_MDNS_ADVERTISE", "true").strip().lower()
        not in ("false", "0")
    )
    # No PHOTO_WALL_HTTP_PORT precedent exists: today the listen port is only
    # known to the `uvicorn --port` invocation outside this module (see
    # Dockerfile), never passed into create_app(). Advertising needs it, so
    # this introduces the one new config knob, defaulting to the port the
    # shipped Dockerfile's uvicorn CMD already binds (8000).
    mdns_port = mdns_port or int(os.environ.get("PHOTO_WALL_HTTP_PORT", "8000"))
    mdns_advertiser = mdns_advertiser or (
        MdnsCentralAdvertiser(port=mdns_port) if mdns_enabled else None
    )
    scheduler_health = {
        "enabled": run_scheduler,
        "running": False,
        "status": "starting" if run_scheduler else "disabled",
        "last_tick": None,
        "last_tick_monotonic": None,
        "error": None,
    }

    async def scheduler():
        scheduler_health["running"] = True
        scheduler_health["status"] = "starting"
        try:
            while True:
                try:
                    projection = await asyncio.to_thread(coordinator.advance)
                    await asyncio.to_thread(
                        media_application.request_acquisitions, projection.acquisitions
                    )
                    scheduler_health.update(
                        last_tick=clock.utc(),
                        last_tick_monotonic=clock.monotonic(),
                        error=None,
                        status="ok",
                    )
                except Exception:
                    # Sanitized health only: driver exceptions may include connection secrets.
                    scheduler_health.update(
                        error="coordination_unavailable", status="coordination_unavailable"
                    )
                await asyncio.sleep(1)
        finally:
            scheduler_health.update(running=False, status="stopped")

    @asynccontextmanager
    async def lifespan(app):
        db.migrate()
        if isinstance(media_queue, ProcrastinateMediaQueue):
            media_queue.apply_schema(db.dsn)
        # Real network registration (join multicast group, register the
        # service) can be slow -- or simply hang -- on a constrained Docker
        # bridge network. Advertising is a convenience for discovery, never
        # a serving requirement (mdns_advertiser.start() already treats
        # registration failure as best-effort), so it must not delay
        # central becoming ready: run it in the background instead of
        # awaiting it before yield.
        mdns_advertise_task = (
            asyncio.create_task(mdns_advertiser.start())
            if mdns_enabled and mdns_advertiser is not None
            else None
        )
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
            if mdns_enabled and mdns_advertiser is not None:
                if mdns_advertise_task is not None and not mdns_advertise_task.done():
                    mdns_advertise_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await mdns_advertise_task
                await mdns_advertiser.stop()
            if owns_db:
                db.close()

    app = FastAPI(
        title="Photo Wall",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.registry = registry
    app.state.coordinator = coordinator
    app.state.app_packages = app_packages
    app.state.mdns_advertiser = mdns_advertiser
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

    @app.exception_handler(AppPackageError)
    async def app_package_error(request, exc):
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
            database = db.healthy()
        except Exception:
            database = False
        scheduler = dict(scheduler_health)
        last_tick_monotonic = scheduler["last_tick_monotonic"]
        scheduler.pop("last_tick_monotonic", None)
        if scheduler["enabled"]:
            if scheduler["status"] == "stopped":
                scheduler["status"] = "stopped"
            elif scheduler["error"] is not None:
                scheduler["status"] = scheduler["error"]
            elif last_tick_monotonic is None:
                scheduler["status"] = "starting"
            elif not scheduler["running"]:
                scheduler["status"] = "stopped"
            else:
                try:
                    age = clock.monotonic() - last_tick_monotonic
                except Exception:
                    age = SCHEDULER_MAX_AGE + 1
                scheduler["status"] = "ok" if 0 <= age <= SCHEDULER_MAX_AGE else "stale"
            healthy = database and scheduler["status"] == "ok"
        else:
            scheduler["status"] = "disabled"
            healthy = database
        return JSONResponse(
            {
                "status": "ok" if healthy else "unavailable",
                "database": database,
                "protocol": 1,
                "scheduler": scheduler,
            },
            status_code=200 if healthy else 503,
        )

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(
            Path(__file__).with_name("operator.html"),
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
            },
        )

    @app.get("/operator.js", include_in_schema=False)
    def operator_script():
        return FileResponse(
            Path(__file__).with_name("operator.js"),
            media_type="text/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/v1/enrollment/challenge")
    def challenge(request: Challenge):
        return registry.challenge(request.public_key)

    @app.post("/v1/enrollment/register")
    def register(request: Enrollment):
        return registry.enroll(request)

    @app.get("/v1/app/manifest")
    def app_manifest():
        # Player-facing, unauthenticated (trusted LAN, 0009): the bootstrapper
        # asks this before it has any app code to enroll with.
        return app_packages.current()

    @app.get("/v1/app/package/{sha256}.deb")
    def app_package(sha256: str):
        # Line-for-line analogue of the rootfs route above: same O_NOFOLLOW,
        # fstat, size-match, and bounded streaming discipline. The sha256 here
        # is a corruption check only (0009 owner ruling) -- this route proves
        # the bytes match what central registered, not who authored them.
        package = app_packages.package(sha256)
        if app_root is None:
            raise AppPackageError("app_artifact_unavailable", 503)
        path = app_root / f"app-{sha256}.deb"
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except (FileNotFoundError, OSError):
            raise AppPackageError("app_artifact_unavailable", 503) from None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != package["size"]:
                raise AppPackageError("app_artifact_invalid", 503)
        except Exception:
            os.close(descriptor)
            raise

        def content():
            with os.fdopen(descriptor, "rb") as source:
                while chunk := source.read(1024 * 1024):
                    yield chunk

        return StreamingResponse(
            content(),
            media_type="application/vnd.debian.binary-package",
            headers={
                "Cache-Control": "public, immutable",
                "Content-Length": str(package["size"]),
                "Digest": f"sha-256={base64.b64encode(bytes.fromhex(sha256)).decode()}",
            },
        )

    @app.get("/v1/player/config")
    def player_config(identity: dict = Depends(player)):
        config = coordinator.configuration(identity["id"], identity["authority_epoch"])
        return {**config.model_dump(mode="json"), "server_time": clock.utc()}

    @app.get("/v1/player/state")
    def player_state(identity: dict = Depends(player)):
        return coordinator.delivery(identity["id"], identity["authority_epoch"])

    @app.get("/v1/player/time", response_model=PlayerTime)
    def player_time(identity: dict = Depends(player)):
        return PlayerTime(
            player_id=identity["id"],
            authority_epoch=identity["authority_epoch"],
            server_time=clock.utc(),
        )

    @app.get("/v1/media/{digest}")
    def media_file(
        digest: str,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
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
                state = await asyncio.to_thread(
                    coordinator.delivery, identity["id"], identity["authority_epoch"]
                )
                await websocket.send_json(
                    {
                        "type": "state",
                        "configuration": state["configuration"].model_dump(mode="json"),
                        "plan": state["plan"].model_dump(mode="json") if state["plan"] else None,
                        "commits": [c.model_dump(mode="json") for c in state["commits"]],
                        "revocations": [r.model_dump(mode="json") for r in state["revocations"]],
                    }
                )
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(), timeout=0.5)
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

    @app.get(
        "/v1/operator/inventory",
        dependencies=[Depends(admin)],
        response_model=InstallationInventory,
    )
    def inventory():
        return registry.inventory()

    @app.post("/v1/operator/app", dependencies=[Depends(admin)], status_code=201)
    def register_app(request: AppPackageRegistration):
        # Stage-by-reference, mirroring register_release above: the operator
        # places the `.deb` bytes under PHOTO_WALL_APP_ROOT out of band, and
        # this call records the {version, sha256, size} pointer to them. No
        # bytes cross this request body.
        app_packages.register(request.version, request.sha256, request.size)
        return {"status": "registered"}

    @app.put("/v1/operator/app/current", dependencies=[Depends(admin)])
    def promote_app(request: AppPackagePromotion):
        app_packages.promote(request.sha256)
        return {"status": "configured"}

    @app.post("/v1/operator/frames", dependencies=[Depends(admin)], status_code=201)
    def create_frame(frame: FrameCreate):
        return registry.create_frame(frame)

    @app.put("/v1/operator/frames/{frame_id}/binding", dependencies=[Depends(admin)])
    def bind(frame_id: Identifier, binding: BindingRequest):
        return registry.bind(
            frame_id,
            binding.player_id,
            binding.output_id,
            expected_generation=binding.expected_generation,
        )

    @app.delete("/v1/operator/frames/{frame_id}/binding", dependencies=[Depends(admin)])
    def unbind(frame_id: Identifier, request: UnbindRequest):
        return registry.unbind(frame_id, expected_generation=request.expected_generation)

    @app.post("/v1/operator/frames/{frame_id}/calibration", dependencies=[Depends(admin)])
    def calibrate(frame_id: Identifier, request: CalibrationRequest):
        return registry.calibrate(
            frame_id,
            request.operation,
            request.expected_revision,
            request.calibration,
            expected_generation=request.expected_generation,
        )

    @app.post("/v1/operator/players/{player_id}/retire", dependencies=[Depends(admin)])
    def retire(player_id: Identifier):
        registry.retire(player_id)
        return {"status": "retired"}

    @app.get("/v1/operator/runtime", dependencies=[Depends(admin)])
    def runtime_state():
        runtime = coordinator.runtime.read()
        return {
            "definitions": runtime.export_state()["scenes"],
            "programs": runtime.export_state()["programs"],
            "current": runtime.project(clock.utc()),
        }

    @app.get("/v1/operator/media", dependencies=[Depends(admin)])
    def media_state():
        return {"sources": media_application.sources(), "health": media_application.health()}

    @app.put(
        "/v1/operator/sources/{source_ref}",
        dependencies=[Depends(admin)],
        response_model=SourceConfigurationReceipt,
    )
    def configure_source(source_ref: Identifier, source: SourceSpec):
        if source.source_ref != source_ref:
            raise ValueError("Source identity mismatch")
        return SourceConfigurationReceipt(
            source_ref=source_ref,
            created=media_application.configure_source(source),
        )

    @app.post(
        "/v1/operator/sources/{source_ref}/refresh",
        dependencies=[Depends(admin)],
        status_code=202,
        response_model=RefreshReceipt,
    )
    def refresh_source(source_ref: Identifier):
        return media_application.request_refresh(source_ref)

    @app.get("/v1/operator/sources/{source_ref}/candidates", dependencies=[Depends(admin)])
    def source_candidates(source_ref: Identifier, frame_id: Identifier | None = None):
        profile = registry.frame_profile(frame_id) if frame_id is not None else None
        return media_application.source_candidates(source_ref, profile=profile)

    @app.post("/v1/operator/authored-candidates", dependencies=[Depends(admin)])
    def author_candidates(request: AuthoredCandidatesRequest):
        return media_application.author_authored_candidates(request.source_ref, request.asset_ids)

    @app.put("/v1/operator/scenes/{scene_id}/authored", dependencies=[Depends(admin)])
    def configure_authored_scene(scene_id: Identifier, request: AuthoredSceneRequest):
        if request.scene.scene_id != scene_id:
            raise ValueError("Scene identity mismatch")
        return coordinator.configure_authored_scene(
            request.scene, request.source_ref, request.asset_ids
        )

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
        return coordinator.runtime.command(
            "activate",
            request.scene_id,
            request.activation_id,
            clock.utc(),
            priority=request.priority,
            repeat=request.repeat,
            force=request.force,
            expires_at=request.expires_at,
        )

    @app.post("/v1/operator/runs/{run_id}/{operation}", dependencies=[Depends(admin)])
    def control_run(run_id: Identifier, operation: Literal["finish", "cancel"]):
        return coordinator.runtime.command(operation, run_id, clock.utc())

    return app
