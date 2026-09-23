"""Central operator and enrollment HTTP adapter. Domain authority lives in Registry."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import Field, model_validator

from central import cache_layout
from central.content_catalog.catalog import CatalogError
from central.content_routes import mount_content_routes
from central.content_wiring import ContentServices, build_content_services
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
from central.netboot_base import record_base_health
from central.registry import Enrollment, FrameCreate, FramePlacement, Registry, RegistryError
from central.runtime import Program, Scene
from contracts.models import (
    BaseHealth,
    Calibration,
    Identifier,
    Model,
    Observation,
    PlayerTime,
    Readiness,
)
from contracts.time import Clock, SystemClock
from media.models import SourceSpec

LOG = logging.getLogger("central.app")

SCHEDULER_MAX_AGE = 10.0
# CatalogError kinds -> HTTP status; the body is always {"error": code}.
CATALOG_ERROR_STATUS = {"not_found": 404, "conflict": 409, "invalid": 422}


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


class DevicePin(Model):
    # The tag to pin a device to (0012 bead 7). Bounded here; the catalog checks
    # the tag's shape (422) and that the release and the device exist (404).
    tag: str = Field(min_length=1, max_length=128)


def create_app(
    db: Database | None = None,
    clock: Clock | None = None,
    admin_token: str | None = None,
    *,
    run_scheduler: bool | None = None,
    media_root: Path | None = None,
    media_queue: MediaTaskQueue | None = None,
    content: ContentServices | None = None,
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
    # 0013: the app owns the layout. Every domain root is derived from the ONE
    # optional cache root (PHOTO_WALL_CACHE_ROOT, baked default) as internal
    # constants -- there are no per-domain path envs. A caller may still inject an
    # explicit media root (tests).
    media_root = media_root or cache_layout.media_root()
    # OS images and Player `.deb`s (design §10.5): the catalog, the read-through
    # reader, the pod probe and this process's one OutcomeFeed. create_app binds
    # no job handlers; the worker runs them. Built only against a real Database;
    # tests inject fakes.
    content = content or (
        build_content_services(db, clock, cache_root=cache_layout.cache_root())
        if isinstance(db, Database)
        else None
    )
    feed = content.feed if content is not None else None
    # 0013: media_root is now always derived from the cache root, so the gateway
    # is built whenever the app runs against a real Database -- the same gate the
    # media/release queues use above. (Unit tests that stub the DB out entirely
    # get no gateway, exactly as when media_root was env-gated.)
    media_gateway = (
        MediaGateway(
            MediaStore(
                coordinator.media,
                media_root,
                installation=coordinator.installation,
                execution=PostgresExecutionRepository(),
            )
        )
        if media_root and isinstance(db, Database)
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
        if isinstance(media_queue, ProcrastinateMediaQueue) or (
            content is not None and isinstance(db, Database)
        ):
            # The content publisher defers into procrastinate's tables too.
            ProcrastinateMediaQueue.apply_schema(db.dsn)
        if feed is not None:
            await feed.start()
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
            if feed is not None:
                await feed.stop()
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
    app.state.content = content
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

    @app.exception_handler(CatalogError)
    async def catalog_error(request, exc):
        return JSONResponse({"error": exc.code}, status_code=CATALOG_ERROR_STATUS[exc.kind])

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

    # The redesigned React console (delivery plan Bead 17 cutover) is now the
    # operator surface at `/`. The bundle is BUILT (Vite) into
    # central/console/dist/ locally and in CI, and is git-ignored — never
    # committed. Serving must never require dist/ to exist when create_app() is
    # constructed (the fast Python gate builds no bundle): the shell FileResponse
    # is built per-request and only reads dist/ when the route is hit, and the
    # asset mount uses check_dir=False so an absent dist/ 404s at request time
    # instead of raising at construction.
    console_dist = Path(__file__).with_name("console") / "dist"

    def console_shell() -> FileResponse:
        # Same hardening the legacy flat page carried (former app.py:344-352):
        # `no-store` + the strict same-origin CSP. `script-src 'self'` admits the
        # same-origin bundle the shell loads, so the CSP is unchanged at cutover.
        return FileResponse(
            console_dist / "index.html",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
            },
        )

    @app.get("/", include_in_schema=False)
    def index():
        return console_shell()

    @app.get("/console", include_in_schema=False)
    def console():
        # Cutover alias: the console has always been reachable at `/console` (Bead
        # 0), and existing `/console` browser tests + operator bookmarks keep
        # working now that `/` serves the same shell.
        return console_shell()

    app.mount(
        "/console/assets",
        StaticFiles(directory=console_dist / "assets", check_dir=False),
        name="console_assets",
    )

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

    @app.post("/v1/player/base-health")
    def base_health(request: BaseHealth, identity: dict = Depends(player)):
        # Authenticated by the enrolled-device token (the `player` dependency),
        # but -- unlike readiness -- requires NO live plan offer (0012 r7), so an
        # enrolled-but-unbound, base-booted device can move the latest-verified
        # frontier. Join player_id -> players.device_id -> devices, then advance
        # known-good only for a genuinely healthy, monotonic report whose
        # running_tag equals the tag Central recorded as last-served (E1: the
        # write is a single conditional UPDATE under FOR UPDATE, so a concurrent
        # recovery serve that moved the served tag invalidates a stale write).
        if request.authority_epoch != identity["authority_epoch"]:
            raise RegistryError("stale_authority", 403)
        with db.transaction() as conn:
            row = conn.execute(
                "SELECT device_id FROM players WHERE id=%s", (identity["id"],)
            ).fetchone()
            accepted = (
                record_base_health(conn, row["device_id"], request, clock=clock)
                if row is not None
                else False
            )
        return {"accepted": accepted}

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

    def _content() -> ContentServices:
        if content is None:
            raise RegistryError("content_unavailable", 503)
        return content

    @app.get("/v1/operator/app/releases", dependencies=[Depends(admin)])
    async def list_releases():
        # Discovered releases, semver DESC, each flagged deployable/promoted/has_os_image.
        return [asdict(view) for view in await _content().catalog.releases_view()]

    @app.post("/v1/operator/app/releases/{tag}/promote", dependencies=[Depends(admin)])
    async def promote_release(tag: str):
        # Records the promoted tag and publishes its `.deb` fetch in one transaction
        # (unknown tag -> 404, no `.deb` -> 409). The manifest never waits on the
        # bytes: the package route reads them through the cache.
        await _content().catalog.promote(tag)
        return {"status": "promoted"}

    @app.post("/v1/operator/app/releases/refresh", dependencies=[Depends(admin)])
    async def refresh_releases():
        # Publishes SyncReleases now (merged with a pending tick) so a newly-cut
        # release appears without waiting for the next cadence.
        await _content().catalog.refresh()
        return JSONResponse({"status": "polling"}, status_code=202)

    @app.put("/v1/operator/devices/{device_id}/pin", dependencies=[Depends(admin)])
    async def pin_device(device_id: Identifier, request: DevicePin):
        # Operator pin (0012 bead 7): sets `devices.attached_tag`, which the catalog
        # resolves as the only candidate (a pinned device never gets a substitute),
        # and publishes the tag's OS-image and `.deb` fetches in the same
        # transaction, so a recovery pin comes up on the device's NEXT netboot.
        # Unknown release or device -> 404 with no write.
        await _content().catalog.pin(device_id, request.tag)
        return {"status": "pinned"}

    @app.delete("/v1/operator/devices/{device_id}/pin", dependencies=[Depends(admin)])
    async def unpin_device(device_id: Identifier):
        # Clear the pin: the device falls back to the unpinned precedence.
        await _content().catalog.unpin(device_id)
        return {"status": "cleared"}

    @app.get("/v1/operator/netboot", dependencies=[Depends(admin)])
    async def netboot_status():
        # Read-only operator view: the live frontier and every active device's
        # netboot state (pin, known-good, last served, boot outcome, fence).
        view = await _content().catalog.netboot_view()
        return {"frontier": view.frontier, "devices": [asdict(row) for row in view.devices]}

    @app.post("/v1/operator/frames", dependencies=[Depends(admin)], status_code=201)
    def create_frame(frame: FrameCreate):
        return registry.create_frame(frame)

    @app.patch("/v1/operator/frames/{frame_id}", dependencies=[Depends(admin)])
    def reposition(frame_id: Identifier, placement: FramePlacement) -> dict:
        return registry.place_frame(frame_id, placement)

    @app.delete("/v1/operator/frames/{frame_id}", dependencies=[Depends(admin)])
    def remove_frame(frame_id: Identifier) -> dict:
        # Guard 1 (runtime, in-route, in-memory, cheap): refuse while a live Run
        # targets this Frame. Read the projected runtime exactly as
        # GET /v1/operator/runtime does. run.phase is a plain str on RunView --
        # do NOT use `.active` (that exists only on the internal _Run, not on the
        # projected RunView). participants is built from ALL runs unfiltered, so
        # filtering to the live phases (body, outro) is BOTH correct and required:
        # completed/cancelled runs must not block a delete.
        view = coordinator.runtime.read().project(clock.utc())
        target = f"frame:{frame_id}"
        if any(run.phase in ("body", "outro") and target in run.participants for run in view.runs):
            raise RegistryError("frame_in_use", 409)
        # Guard 2 (binding) is enforced atomically inside the store transaction.
        return registry.delete_frame(frame_id)

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

    if content is not None:
        mount_content_routes(app, content)
    return app
