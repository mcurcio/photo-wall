"""Central operator and enrollment HTTP adapter. Domain authority lives in Registry."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import traceback
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Final, Literal

from fastapi import Depends, FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import Field, model_validator

from central import cache_layout
from central.assets.frame_media import FrameMediaRefused
from central.assets.reader import Unavailable
from central.calibration_target_routes import mount_calibration_target_routes
from central.content_routes import (
    error_response,
    mount_content_routes,
    stream_frame_media,
    until_disconnect,
)
from central.content_wiring import ContentServices, build_content_services
from central.coordination import CoordinationLimits, Coordinator
from central.db import Database, DatabaseTransactionClock, ProcessTransactionClock, TransactionClock
from central.display_routes import mount_display_routes, mount_picture_routes
from central.fleet.leaf_bridge import mount_bus_bridges
from central.fleet.node_routes import mount_node_routes
from central.fleet.node_sessions import NodeControlConfig
from central.frame_lighting.paper_profiles import LUT_RECIPE_ID
from central.home_assistant_routes import mount_home_assistant_routes
from central.house_routes import mount_house_routes
from central.infra.display_store import (
    PgDisplayCommands,
    PgDisplayQueries,
    PgPictureSessions,
    frame_outputs_in,
)
from central.infra.fit_analysis_store import PgFitAnalysisReader
from central.infra.fit_settings_store import PgFitSettings
from central.infra.frame_lighting_store import PgFrameLighting, PgHouseLock
from central.infra.home_assistant import references as home_assistant_references
from central.infra.home_assistant.settings import PgHomeAssistantSettings
from central.infra.house_store import PgHouse, seed_house
from central.infra.light_sensor_judge import PI_SOURCE, is_player
from central.infra.paper_profile_store import PgPaperProfiles
from central.infra.photo_library_store import PgPhotoLibrary
from central.infra.power_store import PgPower
from central.infra.room_light_store import PgRoomLights
from central.infra.tuning_history_store import PgTuningHistory
from central.infra.why_store import PgWhy
from central.installation_models import FrameName, InstallationInventory, WallInventory, WallNaming
from central.library_routes import mount_library_routes
from central.media_ports import MediaApplication
from central.media_queue import MediaTaskQueue, ProcrastinateMediaQueue
from central.media_repository import MediaRepository
from central.node_runtime_reconciliation import NodeRuntimeReconciler
from central.operator_auth import OperatorAuth
from central.operator_snapshot import OperatorSnapshot, OperatorSnapshotReader, runtime_document
from central.paper_profile_routes import mount_paper_profile_routes
from central.photo_fit_routes import mount_photo_fit_routes
from central.photo_library.provisioning import apply_setting, read_setting
from central.photo_library_routes import mount_photo_library_routes, mount_photo_source_routes
from central.photo_sources import PhotoSources
from central.player_control_protocol import project_state, state_digest
from central.power_routes import mount_power_routes
from central.program_routes import mount_program_routes
from central.registry import (
    Enrollment,
    FrameCreate,
    FramePlacement,
    FrameProfileReplacement,
    FrameTurnChange,
    FrameVideoChange,
    Registry,
    RegistryError,
)
from central.room_light.model import RoomLightRefusal
from central.room_light_routes import mount_room_light_routes
from central.runtime import RuntimeConflict, Scene, SceneInUse, SceneOnWall, SceneWriteRefused
from central.runtime_store import RuntimeStore
from central.source_names import SourceInUse, SourceNameService
from central.tuning_history_routes import mount_tuning_history_routes
from central.wall_plan.fit import fit_target
from central.wall_plan.fit_analysis import analyse
from central.wall_plan.fit_settings import FIT_SETTING_RANGES
from central.why_routes import mount_why_routes
from contracts.central_identity import LOCATE_PATH, identity_body
from contracts.models import (
    Identifier,
    Model,
    Observation,
    PlayerTime,
    Readiness,
)
from contracts.player_control import ControlAck, ControlHello
from contracts.time import Clock, SystemClock

LOG = logging.getLogger("central.app")

SCHEDULER_MAX_AGE = 10.0
_OWN_PACKAGES = ("central", "media", "contracts", "nodeapi")


def scheduler_failure(exc: BaseException) -> str:
    """A scheduler tick's failure as its exception type and the innermost line of this project's
    code it passed (else the innermost line at all), e.g. `ValidationError at
    central/coordination.py:231`. Never the message: a driver's may carry a connection string."""
    frames = traceback.extract_tb(exc.__traceback__)
    own = [frame for frame in frames if "site-packages" not in frame.filename
           and any(f"/{package}/" in frame.filename for package in _OWN_PACKAGES)]
    if not frames:
        return type(exc).__name__
    where = (own or frames)[-1]
    parts = Path(where.filename).parts
    start = max((i for i, part in enumerate(parts) if part in _OWN_PACKAGES), default=len(parts) - 1)
    return f"{type(exc).__name__} at {'/'.join(parts[start:])}:{where.lineno}"


class Challenge(Model):
    public_key: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class BindingRequest(Model):
    player_id: Identifier
    output_id: Identifier
    expected_generation: int = Field(ge=0)


class UnbindRequest(Model):
    expected_generation: int = Field(ge=0)


class ActivationRequest(Model):
    scene_id: Identifier
    activation_id: Identifier
    priority: int = Field(default=0, ge=0)   # above the Default Scene (central.runtime DEFAULT_SCENE_RANK)
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




class ReportSourceForms:
    """room_light.ports.ReportSources over each source Central runs and its adapter's own check of an
    address's form."""

    def __init__(self, forms: Mapping[str, Callable[[str], bool]]) -> None:
        self._forms = dict(forms)

    def address_refusal(self, source: str, address: str) -> RoomLightRefusal | None:
        form = self._forms.get(source)
        return None if form is not None and form(address) else RoomLightRefusal.UNKNOWN_ADDRESS


REPORT_SOURCES: Final = ReportSourceForms({home_assistant_references.SOURCE: home_assistant_references.is_entity,
                                           PI_SOURCE: is_player})


def create_app(
    db: Database | None = None,
    clock: Clock | None = None,
    admin_token: str | None = None,
    *,
    run_scheduler: bool | None = None,
    cache_root: Path | None = None,
    media_queue: MediaTaskQueue | None = None,
    content: ContentServices | None = None,
    node_control: NodeControlConfig = NodeControlConfig(),
    media_times: TransactionClock | None = None,
    hub_leaf_url: str | None = None,
) -> FastAPI:
    run_scheduler = clock is None if run_scheduler is None else run_scheduler
    clock_injected = clock
    owns_db = db is None
    db = db or Database(os.environ["PHOTO_WALL_DATABASE_URL"])
    clock = clock or SystemClock()
    admin_token = admin_token or os.environ["PHOTO_WALL_ADMIN_TOKEN"]
    if len(admin_token) < 32:
        raise ValueError("PHOTO_WALL_ADMIN_TOKEN must contain at least 32 characters")
    house_lock = PgHouseLock()   # the house's lock and "which Frames name it"
    registry = Registry(db, clock, lighting=PgFrameLighting(LUT_RECIPE_ID))
    media_queue = media_queue or (
        ProcrastinateMediaQueue(db.dsn) if isinstance(db, Database) else None
    )
    # G11: media times are the database's in production. A test that injects its own
    # clock (as run_scheduler above) keeps them on it unless it passes media_times.
    media_times = media_times or (
        DatabaseTransactionClock() if clock_injected is None else ProcessTransactionClock(clock_injected))
    media_repository = MediaRepository(db, clock, times=media_times, queue=media_queue)
    media_application: MediaApplication = media_repository
    # Follow-ups B1: the photo fit settings' one store (the Planner, the authoring check, the
    # candidates read, the settings routes and the fit analysis all read it).
    fit_settings = PgFitSettings(db, times=media_times)
    # Follow-ups B2: the one fit analysis read (every console moment asks it).
    fit_analysis_reader = PgFitAnalysisReader(db, times=media_times)
    coordinator = Coordinator(
        db,
        clock,
        CoordinationLimits(
            horizon_seconds=float(os.environ.get("PHOTO_WALL_HORIZON_SECONDS", "300")),
            renewal_seconds=float(os.environ.get("PHOTO_WALL_RENEWAL_SECONDS", "30")),
        ),
        media=media_repository,
        fit=fit_settings,
    )
    runtime_store = RuntimeStore(db, clock)
    operator_snapshot = OperatorSnapshotReader(
        db, clock, registry, runtime_store, media_repository, coordinator
    )
    # Roadmap 4: the photo library store (connections, lists, drafts, pools); its times are
    # always the database's, and it queues connection checks and pool steps on its own queue.
    photo_library = PgPhotoLibrary(
        db, times=DatabaseTransactionClock(),
        queue=ProcrastinateMediaQueue(db.dsn) if isinstance(db, Database) else None)
    source_names = SourceNameService(db, media_repository, runtime_store, photo_library)
    photo_sources = PhotoSources(photo_library, source_names)
    # 0013: the app owns the layout. Every domain root is derived from the ONE
    # optional cache root (PHOTO_WALL_CACHE_ROOT, baked default) as internal
    # constants -- there are no per-domain path envs. A caller may still inject an
    # explicit cache root (tests).
    cache_root = cache_root or cache_layout.cache_root()
    # OS images and Player `.deb`s (design §10.5): the catalog, the read-through
    # reader, the pod probe and this process's one OutcomeFeed. create_app binds
    # no job handlers; the worker runs them. Built only against a real Database;
    # tests inject fakes.
    content = content or (
        build_content_services(
            db, clock, cache_root=cache_root,
            servable_thumbnail=lambda asset_id: (
                photo_library.pool_thumbnail(asset_id) is not None))
        if isinstance(db, Database)
        else None
    )
    feed = content.feed if content is not None else None
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
        logged = None   # the failure last logged, so a failing tick logs once, not every second
        try:
            while True:
                try:
                    await asyncio.to_thread(app.state.node_reconciler.advance)
                    projection = await asyncio.to_thread(coordinator.advance)
                    await asyncio.to_thread(
                        media_application.request_measurements, projection.measurements
                    )
                    scheduler_health.update(
                        last_tick=clock.utc(),
                        last_tick_monotonic=clock.monotonic(),
                        error=None,
                        status="ok",
                    )
                    logged = None
                except Exception as exc:
                    # Sanitized health and log: driver exceptions may include connection secrets.
                    failure = scheduler_failure(exc)
                    if failure != logged:
                        LOG.error("scheduler tick failed: %s", failure)
                        logged = failure
                    scheduler_health.update(
                        error="coordination_unavailable", status="coordination_unavailable"
                    )
                await asyncio.sleep(1)
        finally:
            scheduler_health.update(running=False, status="stopped")

    @asynccontextmanager
    async def lifespan(app):
        db.migrate()
        if isinstance(db, Database):    # Settings › House starts from Central's own zone (a stand-in has none)
            seed_house(db, DatabaseTransactionClock())
        if isinstance(media_queue, ProcrastinateMediaQueue) or (
            content is not None and isinstance(db, Database)
        ):
            # The content publisher defers into procrastinate's tables too.
            ProcrastinateMediaQueue.apply_schema(db.dsn)
        if isinstance(db, Database):
            # The Immich connection the deployment sets, kept in step at every start.
            apply_setting(photo_library, read_setting(
                os.environ, lambda path: Path(path).read_text(encoding="utf-8")))
        if feed is not None:
            await feed.start()
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
    app.state.operator_snapshot = operator_snapshot
    app.state.media_repository = media_repository
    app.state.photo_library = photo_library
    app.state.content = content
    bearer = HTTPBearer(auto_error=False)
    # The operator principal (pass A): bearer or signed session cookie. The key derives from the
    # token here, once, so a wrong-length or unencodable token fails at construction.
    operator_auth = OperatorAuth(admin_token, clock)
    app.state.operator_auth = operator_auth
    admin = operator_auth.admin

    def player(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if not credentials:
            raise RegistryError("unauthorized", 401)
        return registry.authenticate(credentials.credentials)

    def control_state(identity: dict) -> dict:
        # Projection and challenge issuance share one Coordination transaction.
        def finalize(conn, delivery: dict) -> dict:
            selection = registry.control_selection_in(
                conn, identity["id"], identity["authority_epoch"]
            )
            payload = project_state(delivery, selection)
            cue = payload["identify_output"]
            payload["identify_expires_at"] = (
                clock.utc() + cue["remaining_seconds"] if cue else None
            )
            digest = state_digest(payload)
            payload.update(registry.issue_control_delivery_record_in(
                conn, identity["id"], identity["authority_epoch"], digest
            ))
            return payload

        return coordinator.delivery(
            identity["id"], identity["authority_epoch"], finalize=finalize
        )

    @app.exception_handler(RegistryError)
    async def registry_error(request, exc):
        detail = {"error": exc.code}
        if isinstance(exc, SourceInUse):
            detail["scene_ids"] = exc.scene_ids
        detail.update(exc.details)
        return JSONResponse(detail, status_code=exc.status)

    @app.exception_handler(RuntimeConflict)
    async def runtime_conflict(request, exc):
        return JSONResponse({"error": exc.code}, status_code=409)

    @app.exception_handler(SceneWriteRefused)
    async def scene_write_refused(request, exc):
        if exc.reason == "changed":
            return JSONResponse({"error": "changed", "current": exc.current.model_dump(mode="json")},
                                status_code=409)
        return JSONResponse({"error": exc.reason, "max_seconds": exc.max_seconds}, status_code=422)

    @app.exception_handler(SceneInUse)
    async def scene_in_use(request, exc):
        return JSONResponse({
            "error": exc.code,
            "program_ids": exc.program_ids,
            "run_ids": exc.run_ids,
            "queued_activation_ids": exc.queued_activation_ids,
            "scene_ids": exc.scene_ids,
            "default_scene": exc.default_scene,
        }, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Do not reflect request bodies, private keys, token strings, or upstream fields.
        return JSONResponse({"error": "invalid_request"}, status_code=422)

    @app.exception_handler(ValueError)
    async def invalid_command(request, exc):
        return JSONResponse({"error": "invalid_command"}, status_code=422)

    # The session routes, `no-store` on every operator response, and the unhandled-500 handler.
    operator_auth.mount(app)

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

    @app.get(LOCATE_PATH)
    def central_identity():
        # Where a device's locate chain ends (decision 0014): no database, catalog or clock,
        # so it answers even when /healthz says unavailable; never a redirect; `no-store` so
        # no proxy can pin an identity.
        return Response(identity_body(), media_type="application/json",
                        headers={"Cache-Control": "no-store"})

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

    # Python 3.12's built-in table has no .woff2, and the image's /etc/mime.types is not
    # relied on: the console font must be served as font/woff2 (pass C §5).
    mimetypes.add_type("font/woff2", ".woff2")
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

    @app.post("/v1/player/hello")
    def player_hello(request: ControlHello, identity: dict = Depends(player)):
        if request.authority_epoch != identity["authority_epoch"]:
            raise RegistryError("stale_authority", 403)
        return registry.control_hello(identity["id"], request)

    @app.get("/v1/player/config")
    def player_config(identity: dict = Depends(player)):
        config = coordinator.configuration(identity["id"], identity["authority_epoch"])
        return {**config.model_dump(mode="json"), "server_time": clock.utc()}

    @app.get("/v1/player/state")
    def player_state(identity: dict = Depends(player)):
        return control_state(identity)

    @app.post("/v1/player/control-acks")
    def control_ack(request: ControlAck, identity: dict = Depends(player)):
        if request.authority_epoch != identity["authority_epoch"]:
            raise RegistryError("stale_authority", 403)
        return registry.control_ack_response(identity["id"], request).model_dump(
            mode="json", by_alias=True, exclude_none=True)

    @app.get("/v1/player/time", response_model=PlayerTime)
    def player_time(identity: dict = Depends(player)):
        return PlayerTime(
            player_id=identity["id"],
            authority_epoch=identity["authority_epoch"],
            server_time=clock.utc(),
        )

    @app.get("/v1/media/{asset_id}/{recipe_id}")
    async def frame_media_file(
        asset_id: str,
        recipe_id: str,
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        """One media variant, by its name, through the one read path: the grant
        (`FrameMedia`), then the AssetReader (a hit serves, a miss re-makes it and waits). The
        transfer pin is released when the body ends, or at once when nothing is served."""
        if credentials is None:
            raise RegistryError("unauthorized", 401)
        if "range" in request.headers:
            raise RegistryError("media_range_unsupported", 416)
        if content is None or content.frame_media is None or content.frame_media_reader is None:
            raise RegistryError("media_unavailable", 503)
        frame_media, reader = content.frame_media, content.frame_media_reader
        try:
            grant = await asyncio.to_thread(frame_media.resolve, credentials.credentials,
                                            f"{asset_id}.{recipe_id}")
        except FrameMediaRefused as refused:
            return error_response(refused.code, refused.status)
        try:
            opened = await until_disconnect(request, reader.read(grant.candidates))
        except BaseException:
            await asyncio.to_thread(frame_media.release, grant.transfer_owner)
            raise
        if isinstance(opened, Unavailable):
            await asyncio.to_thread(frame_media.release, grant.transfer_owner)
            return error_response("media_unavailable", 503,
                                  retry_after=opened.retry_after_seconds)
        return stream_frame_media(opened, release=lambda: frame_media.release(
            grant.transfer_owner))

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
                state = await asyncio.to_thread(control_state, identity)
                # A re-enrollment during projection must not keep this old session alive.
                if await asyncio.to_thread(registry.authenticate, token) != identity:
                    raise RegistryError("stale_authority", 403)
                await websocket.send_json({"type": "state", **state})
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
        # The reports are read after the inventory, so read_at bounds every timestamp in it.
        return registry.inventory().with_liveness(coordinator.player_reports_lock_free())

    @app.get(
        "/v1/operator/snapshot",
        dependencies=[Depends(admin)],
        response_model=OperatorSnapshot,
    )
    def operator_state_snapshot():
        return operator_snapshot.read()

    def _content() -> ContentServices:
        if content is None:
            raise RegistryError("content_unavailable", 503)
        return content

    @app.post("/v1/operator/app/releases/refresh", dependencies=[Depends(admin)])
    async def refresh_releases():
        # Publishes SyncReleases now (merged with a pending tick) so a newly-cut
        # release appears without waiting for the next cadence.
        await _content().catalog.refresh()
        return JSONResponse({"status": "polling"}, status_code=202)

    @app.post("/v1/operator/frames", dependencies=[Depends(admin)], status_code=201)
    def create_frame(frame: FrameCreate):
        return registry.create_frame(frame)

    @app.patch("/v1/operator/frames/{frame_id}", dependencies=[Depends(admin)])
    def reposition(frame_id: Identifier, placement: FramePlacement) -> dict:
        return registry.place_frame(frame_id, placement)

    @app.put("/v1/operator/frames/{frame_id}/name", dependencies=[Depends(admin)])
    def rename_frame(frame_id: Identifier, request: FrameName) -> dict:
        return registry.rename_frame(frame_id, request.name)

    @app.put("/v1/operator/walls/{surface_id}", dependencies=[Depends(admin)], response_model=WallInventory)
    def name_wall(surface_id: Identifier, naming: WallNaming) -> WallInventory:
        return registry.name_wall(surface_id, naming)

    def _refuse_frame_in_use(frame_id: str) -> None:
        # Runs refer to Frame ids by string, so keep destructive/reconfiguring
        # operator actions away from a target while its projected Run is live.
        view = coordinator.runtime.read().project(clock.utc())
        target = f"frame:{frame_id}"
        if any(run.phase in ("body", "outro") and target in run.participants for run in view.runs):
            raise RegistryError("frame_in_use", 409)

    @app.put("/v1/operator/frames/{frame_id}/profile", dependencies=[Depends(admin)])
    def replace_frame_profile(frame_id: Identifier, request: FrameProfileReplacement) -> dict:
        # Keep the live-Run check and Frame update in one serialized transaction.
        # Otherwise an activation could commit between the check and profile write.
        with coordinator.serialized_runtime_read() as (conn, runtime):
            references = runtime.frame_references(frame_id, clock.utc())
            if references["run_ids"]:
                raise RegistryError("frame_in_use", 409)
            return registry.replace_frame_profile(
                frame_id, request.profile, expected_generation=request.expected_generation,
                conn=conn,
            )

    @app.put("/v1/operator/frames/{frame_id}/turn", dependencies=[Depends(admin)])
    def turn_frame(frame_id: Identifier, request: FrameTurnChange) -> dict:
        # How the Frame's screen hangs (render redesign S2): the Frame's own setting, sent in its
        # Output's document; Registry owns the write, the generation and the Node's wake.
        return registry.turn_frame(frame_id, request.turn, expected_generation=request.expected_generation)

    @app.put("/v1/operator/frames/{frame_id}/video", dependencies=[Depends(admin)])
    def set_frame_video(frame_id: Identifier, request: FrameVideoChange) -> dict:
        # Shows videos (frame box run, fb-5): the Frame's own setting, last write wins; it changes
        # what the Planner may put on the Frame, never its picture, so no generation is asked.
        return registry.set_frame_video(frame_id, request.video)

    @app.delete("/v1/operator/frames/{frame_id}", dependencies=[Depends(admin)])
    def remove_frame(frame_id: Identifier) -> dict:
        # Serialize with all Runtime/Scene mutations, then take the Frame row lock,
        # matching authored configuration's COORDINATION -> RUNTIME -> Frame order.
        # Registry checks existence and binding before applying reference data so those
        # established errors retain precedence over reference conflicts.
        with coordinator.serialized_runtime_read() as (conn, runtime):
            references = runtime.frame_references(frame_id, clock.utc())
            if references["run_ids"]:
                raise RegistryError("frame_in_use", 409, details={
                    "run_ids": list(references["run_ids"]),
                })

            return registry.delete_frame(frame_id, conn=conn, references=references)

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

    @app.post("/v1/operator/players/{player_id}/retire", dependencies=[Depends(admin)])
    def retire(player_id: Identifier):
        registry.retire(player_id)
        return {"status": "retired"}

    @app.post("/v1/operator/players/{player_id}/outputs/{output_id}/identify",
              dependencies=[Depends(admin)])
    def identify_output(player_id: Identifier, output_id: Identifier):
        return registry.identify_output(player_id, output_id)

    @app.get("/v1/operator/runtime", dependencies=[Depends(admin)])
    def runtime_state():
        read_at = clock.utc()
        return runtime_document(coordinator.runtime.read(), read_at)

    @app.get("/v1/operator/media", dependencies=[Depends(admin)])
    def media_state():
        return media_application.media_read()

    @app.delete("/v1/operator/source-names/{name}", dependencies=[Depends(admin)])
    def delete_source_name(name: str, expected_revision: int = Query(ge=1)):
        return source_names.delete(name, expected_revision)

    @app.get("/v1/operator/sources/{source_ref}/candidates", dependencies=[Depends(admin)])
    def source_candidates(
        source_ref: Identifier, frame_id: Identifier | None = None,
        crop_limit: float | None = Query(default=None, ge=FIT_SETTING_RANGES["crop_limit"][0],
                                         le=FIT_SETTING_RANGES["crop_limit"][1]),
    ):
        # Follow-ups B1: the Frame as the fit rule sees it (its Frame output's `fit_target`),
        # judged with the saved photo fit settings; no crop limit given is the house's. A Frame
        # with no known screen has no shape, so nothing is judged for it (frame box run fb-8).
        target = None
        if frame_id is not None:
            with db.transaction() as conn:
                output = next((output for output in frame_outputs_in(conn)
                               if output.frame_id == frame_id), None)
            if output is None:
                raise RegistryError("unknown_frame", 404)
            target = fit_target(output)
            if target is None:
                raise RegistryError("frame_size_unknown", 409)
        return media_application.source_candidates(source_ref, target=target,
                                                   crop_limit=crop_limit,
                                                   settings=fit_settings.fit_settings())

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

    # A Scene's two writes, one Runtime command each, both answering a `SceneOnWall` (what on the
    # wall still plays an earlier version): Save stores it (R4: edits default to the next Run);
    # Save and apply now also replaces at once what plays an earlier version.
    @app.put("/v1/operator/scenes/{scene_id}", dependencies=[Depends(admin)], response_model=SceneOnWall)
    def configure_scene(scene_id: Identifier, scene: Scene):
        if scene.scene_id != scene_id:
            raise ValueError("Scene identity mismatch")
        return coordinator.runtime.command_current("save_scene", scene)

    @app.post("/v1/operator/scenes/{scene_id}/apply-now", dependencies=[Depends(admin)],
              response_model=SceneOnWall)
    def apply_scene_now(scene_id: Identifier, scene: Scene):
        if scene.scene_id != scene_id:
            raise ValueError("Scene identity mismatch")
        return coordinator.runtime.command_current("save_scene_and_apply_now", scene)

    @app.post("/v1/operator/scenes/{scene_id}/authored/apply-now", dependencies=[Depends(admin)])
    def apply_authored_scene_now(scene_id: Identifier, request: AuthoredSceneRequest):
        if request.scene.scene_id != scene_id:
            raise ValueError("Scene identity mismatch")
        return coordinator.configure_authored_scene_and_apply_now(
            request.scene, request.source_ref, request.asset_ids
        )

    @app.delete("/v1/operator/scenes/{scene_id}", dependencies=[Depends(admin)])
    def delete_scene(scene_id: Identifier, expected_revision: int = Query(ge=1)):
        coordinator.runtime.command("delete_scene", scene_id, expected_revision)
        return {"status": "deleted"}

    @app.post("/v1/operator/activations", dependencies=[Depends(admin)])
    def activate(request: ActivationRequest):
        return coordinator.runtime.command_current(
            "activate",
            request.scene_id,
            request.activation_id,
            priority=request.priority,
            repeat=request.repeat,
            force=request.force,
            expires_at=request.expires_at,
        )

    @app.post("/v1/operator/runs/{run_id}/{operation}", dependencies=[Depends(admin)])
    def control_run(run_id: Identifier, operation: Literal["finish", "cancel"]):
        return coordinator.runtime.command_current(operation, run_id)

    mount_photo_library_routes(app, admin=admin, queries=photo_library, commands=photo_library)
    mount_photo_source_routes(app, admin=admin, queries=photo_sources, commands=photo_sources)
    mount_photo_fit_routes(
        app, admin=admin, settings=fit_settings,
        analysis=lambda request: analyse(request, fit_analysis_reader, fit_settings.fit_settings()))
    mount_library_routes(app, admin=admin, library=photo_library, content=content)
    mount_display_routes(app, admin=admin, queries=PgDisplayQueries(db), commands=PgDisplayCommands(db))
    power = PgPower(db, times=media_times)
    mount_power_routes(app, admin=admin, queries=power, commands=power)
    mount_program_routes(app, admin=admin, runtime=runtime_store)
    mount_why_routes(app, admin=admin, queries=PgWhy(db, times=media_times))
    house = PgHouse(db, times=media_times)
    mount_house_routes(app, admin=admin, queries=house, commands=house)
    room_lights = PgRoomLights(db, house_lock)
    mount_room_light_routes(app, admin=admin, lights=room_lights, sources=REPORT_SOURCES)
    mount_home_assistant_routes(app, admin=admin, settings=PgHomeAssistantSettings(db))
    pictures = PgPictureSessions(db)
    mount_picture_routes(app, admin=admin, queries=pictures, commands=pictures)
    mount_tuning_history_routes(app, admin=admin, queries=PgTuningHistory(db))
    mount_calibration_target_routes(app, admin=admin)
    mount_paper_profile_routes(app, admin=admin, player=player,
                               profiles=PgPaperProfiles(db, clock, house_lock))
    if content is not None:
        mount_content_routes(app, content)
    mount_node_routes(app, db=db, clock=clock, admin=admin, coordinator=coordinator, config=node_control,
                      content=content)
    app.state.node_reconciler = NodeRuntimeReconciler(app.state.node_sessions, coordinator)
    # A Node's leaf and Home Assistant's door arrive at Central's origin (erratum E-E3D-CUT-4): relayed to the hub.
    hub_leaf_url = hub_leaf_url or os.environ.get("PHOTO_WALL_HUB_LEAF_URL")
    if hub_leaf_url:
        mount_bus_bridges(app, hub_leaf_url)
    # Every route is bound: refuse one that `admin` guards outside the cookie's path.
    operator_auth.require_scoped(app)
    return app
