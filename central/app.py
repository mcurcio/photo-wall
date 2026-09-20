"""Central operator and enrollment HTTP adapter. Domain authority lives in Registry."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import Field, model_validator

from central.app_packages import AppPackageError, AppPackages
from central.app_release_queue import AppReleaseTaskQueue, ProcrastinateAppReleaseQueue
from central.app_releases import AppReleaseError, AppReleases
from central.artifact_io import HardenedOpenError, open_regular
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
from central.netboot_base import (
    SERIAL_HEADER,
    base_file_path,
    record_base_health,
    sanitize_serial,
    select_base_for_serial,
)
from central.registry import Enrollment, FrameCreate, FramePlacement, Registry, RegistryError
from central.runtime import Program, Scene
from contracts.models import (
    BaseHealth,
    Calibration,
    Digest,
    Identifier,
    Model,
    Observation,
    PlayerTime,
    Readiness,
)
from contracts.release import MAX_ROOTFS_BYTES
from contracts.time import Clock, SystemClock
from media.models import SourceSpec

LOG = logging.getLogger("central.app")

SCHEDULER_MAX_AGE = 10.0
# Server-side sanity bound on the netboot base squashfs. This IS the client's
# fetch cap, not a coincidentally-equal copy: the initrd passes
# contracts.release.MAX_ROOTFS_BYTES as its download bound, so a base larger
# than this can never be booted anyway. Sharing the one constant means nudging
# the cap can't silently desync the server bound from the client's.
MAX_NETBOOT_BASE_BYTES = MAX_ROOTFS_BYTES


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
    release_queue: AppReleaseTaskQueue | None = None,
    app_root: Path | None = None,
    base_root: Path | None = None,
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
    # Where the netboot base squashfs + its build-time SHA256SUMS are staged out
    # of band (same stage-by-reference division of labor as PHOTO_WALL_APP_ROOT);
    # unset -> GET /v1/netboot/base answers 503, never crashes create_app.
    base_root = base_root or (
        Path(os.environ["PHOTO_WALL_BASE_ROOT"]) if "PHOTO_WALL_BASE_ROOT" in os.environ else None
    )
    app_packages = AppPackages(db, clock)
    # GitHub release sourcing (0010) is the same OPT-IN gate the worker uses
    # (bead 3): the feature is live only when PHOTO_WALL_APP_ROOT is set (the
    # shared storage the mirror lands `.deb` bytes into and the serving route
    # reads). When unset, `app_root` is None, no enqueue port is built, and the
    # operator release routes answer 503 "release sourcing not configured"
    # rather than crashing create_app.
    app_releases = AppReleases(db, clock)
    # Producer-side enqueue port, mirroring how ProcrastinateMediaQueue is built
    # and injected above: promote defers a tag-keyed mirror, refresh defers a
    # coalesced poll -- both onto APP_RELEASE_QUEUE, executed by the worker.
    # The same enqueue port also defers the 0012 per-version base fetch, so it is
    # built when EITHER the `.deb` mirror (PHOTO_WALL_APP_ROOT) or base serving
    # (PHOTO_WALL_BASE_ROOT) is configured. The `.deb` operator routes still gate
    # independently on `app_root`, so a base-only deployment does not expose them.
    release_queue = release_queue or (
        ProcrastinateAppReleaseQueue(db.dsn)
        if isinstance(db, Database) and (app_root is not None or base_root is not None)
        else None
    )
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

    @app.exception_handler(AppReleaseError)
    async def app_release_error(request, exc):
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
        try:
            descriptor, metadata = open_regular(path, expected_size=package["size"])
        except HardenedOpenError as error:
            raise AppPackageError(f"app_artifact_{error.reason}", 503) from None

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

    @app.get("/v1/netboot/base")
    def netboot_base(request: Request):
        # Player-facing, UNAUTHENTICATED (trusted LAN, 0009 -- same posture as
        # /v1/app/manifest and the .deb route): a Pi has no credential before it
        # boots. The Pi self-identifies by serial; Central maps it to a canonical
        # `devices` row and resolves ONE tag by per-device precedence (pin, else
        # latest-verified, else -- empty state only -- latest-discovered), then
        # serves that version's immutable base-<tag>.squashfs. Sanitize BEFORE
        # anything consumes the serial: the validated value feeds both the log and
        # the selection seam. The read-modify-write (device upsert + a 200-only
        # last-served record) runs inside the transaction; a miss enqueues a
        # coalesced fetch and 503s, writing NO last-served record so a self-healing
        # retry is never mistaken for a failed boot.
        serial = sanitize_serial(request.headers.get(SERIAL_HEADER))
        LOG.info("netboot base fetch: serial=%s", serial or "<absent-or-invalid>")
        if base_root is None:
            raise AppPackageError("base_artifact_unavailable", 503)
        with db.transaction() as conn:
            decision = select_base_for_serial(conn, serial, clock=clock)
            if decision.fetch_tag is not None and release_queue is not None:
                # Lazy backstop: coalesced by the base:<tag> queueing lock so a
                # burst of retrying Pis collapses to one in-flight fetch.
                release_queue.enqueue_base_fetch_in(conn, decision.fetch_tag)
        if decision.served_tag is None:
            # Nothing resolvable yet (no frontier, no discoverable base).
            raise AppPackageError("base_artifact_unavailable", 503)
        if not decision.cached:
            # Bytes not yet cached: fail closed, the Pi reboots and retries.
            raise AppPackageError("base_artifact_uncached", 503)
        # O_NOFOLLOW open, fstat regular-file + size bound, bounded 1 MiB
        # streaming, and a base64 `Digest` header -- the same discipline as the
        # .deb route. The Digest is the version's recorded squashfs sha, so bytes
        # and Digest agree by construction.
        path = base_file_path(base_root, decision.served_tag)
        try:
            descriptor, metadata = open_regular(path, max_size=MAX_NETBOOT_BASE_BYTES)
        except HardenedOpenError as error:
            raise AppPackageError(f"base_artifact_{error.reason}", 503) from None

        def content():
            with os.fdopen(descriptor, "rb") as source:
                while chunk := source.read(1024 * 1024):
                    yield chunk

        return StreamingResponse(
            content(),
            media_type="application/octet-stream",
            headers={
                "Cache-Control": "public, immutable",
                "Content-Length": str(metadata.st_size),
                "Digest": "sha-256="
                + base64.b64encode(bytes.fromhex(decision.squashfs_sha256)).decode(),
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

    def _release_sourcing() -> AppReleaseTaskQueue:
        # 0010 gate: the release routes exist only when PHOTO_WALL_APP_ROOT is
        # configured (app_root + an enqueue port). Unconfigured -> a clean 503,
        # never a crash. This coexists with the manual POST /v1/operator/app and
        # PUT /v1/operator/app/current escape hatches (0010 decision #6).
        if app_root is None or release_queue is None:
            raise AppReleaseError("release_sourcing_unconfigured", 503)
        return release_queue

    @app.get("/v1/operator/app/releases", dependencies=[Depends(admin)])
    def list_releases():
        _release_sourcing()
        # Discovered list, semver-ordered; each row flagged deployable/promoted/
        # current (current = this row's mirrored bytes are the served ones).
        return app_releases.list()

    @app.post("/v1/operator/app/releases/{tag}/promote", dependencies=[Depends(admin)])
    def promote_release(tag: str):
        queue = _release_sourcing()
        # set_promoted records the operator's chosen tag under FOR UPDATE on the
        # app_release_policy singleton (serializing against any concurrent promote
        # or the worker's reconcile) and reports whether the bytes are already
        # registered. Unknown tag -> 404, undeployable -> 409 (AppReleaseError).
        already_mirrored = app_releases.set_promoted(tag)
        if already_mirrored:
            # Fast path: bytes present, so advance `current` in-request via the
            # single reconcile path (a DB pointer flip under the same lock; no
            # network, no worker). reconcile reads the *current* promoted_tag, so
            # it never advances off a stale value.
            app_releases.reconcile(app_packages)
            return {"status": "promoted"}
        # Lazy mirror: the bytes are absent, so defer a tag-keyed mirror onto the
        # worker's queue and report pending. The worker downloads + verifies +
        # registers, then its own reconcile advances `current` once the bytes land.
        with db.transaction() as conn:
            queue.enqueue_mirror_in(conn, tag)
        return JSONResponse({"status": "pending"}, status_code=202)

    @app.post("/v1/operator/app/releases/refresh", dependencies=[Depends(admin)])
    def refresh_releases():
        queue = _release_sourcing()
        # Defer an on-demand poll (coalesced with the periodic tick) so a
        # newly-cut release appears without waiting for the next cadence.
        with db.transaction() as conn:
            queue.enqueue_poll_in(conn)
        return JSONResponse({"status": "polling"}, status_code=202)

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

    return app
