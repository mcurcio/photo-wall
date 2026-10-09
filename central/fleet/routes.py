"""Fleet HTTP seam: serial-only OS check-ins (observational) and the operator's fleet status."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from central.content_routes import error_response
from central.db import Database
from central.fleet.models import CheckIn, CheckInV2, FleetError
from central.fleet.service import FleetService
from contracts.time import Clock


def _retry_after(clock: Clock) -> int:
    return max(1, 86400 - int(clock.utc()) % 86400)


def mount_fleet_routes(app: FastAPI, *, db: Database, clock: Clock,
                       admin: Callable[..., None]) -> FleetService:
    """Add the T0 check-in and the authenticated status routes."""
    service = FleetService(db, clock)

    @app.exception_handler(FleetError)
    async def fleet_error(_request: Request, exc: FleetError) -> JSONResponse:
        retry_after = _retry_after(clock) if exc.status == 429 else exc.retry_after
        return error_response(exc.code, exc.status, retry_after=retry_after)

    @app.post("/v1/appliance/check-ins")
    async def check_in(body: CheckIn) -> dict:
        return await asyncio.to_thread(service.record_check_in, body)

    @app.post("/v2/appliance/check-ins")
    async def check_in_v2(body: CheckInV2) -> dict:
        return await asyncio.to_thread(service.record_check_in, body)

    @app.get("/v1/operator/fleet", dependencies=[Depends(admin)])
    async def status() -> dict:
        return await asyncio.to_thread(service.status)

    return service
