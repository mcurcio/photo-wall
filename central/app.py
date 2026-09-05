"""Central operator and enrollment HTTP adapter. Domain authority lives in Registry."""

from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field

from central.db import Database
from central.registry import Enrollment, FrameCreate, Registry, RegistryError
from contracts.models import Calibration, Identifier, Model
from contracts.time import Clock, SystemClock


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


def create_app(db: Database | None = None, clock: Clock | None = None,
               admin_token: str | None = None) -> FastAPI:
    db = db or Database(os.environ["PHOTO_WALL_DATABASE_URL"])
    clock = clock or SystemClock()
    admin_token = admin_token or os.environ["PHOTO_WALL_ADMIN_TOKEN"]
    if len(admin_token) < 32:
        raise ValueError("PHOTO_WALL_ADMIN_TOKEN must contain at least 32 characters")
    registry = Registry(db, clock)

    @asynccontextmanager
    async def lifespan(app):
        db.migrate()
        yield

    app = FastAPI(title="Photo Wall", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.registry = registry
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

    @app.get("/healthz")
    def health():
        try:
            healthy = db.healthy()
        except Exception:
            healthy = False
        return JSONResponse({"status": "ok" if healthy else "unavailable", "database": healthy,
                             "protocol": 1}, status_code=200 if healthy else 503)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(Path(__file__).with_name("operator.html"),
                            headers={"Cache-Control": "no-store",
                                     "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"})

    @app.post("/v1/enrollment/challenge")
    def challenge(request: Challenge):
        return registry.challenge(request.public_key)

    @app.post("/v1/enrollment/register")
    def register(request: Enrollment):
        return registry.enroll(request)

    @app.get("/v1/player/config")
    def player_config(identity: dict = Depends(player)):
        config = registry.configuration_for(identity["id"], identity["authority_epoch"])
        return {"protocol": 1, "player_id": identity["id"],
                "authority_epoch": identity["authority_epoch"],
                "bindings": [b.model_dump() for b in config["bindings"]],
                "execution_bindings": [b.model_dump() for b in config["execution_bindings"]],
                "server_time": clock.utc()}

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

    return app
