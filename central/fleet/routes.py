"""Fleet HTTP seam. Serial-only boot traffic is observational; operator writes use admin auth.

The composition root supplies the existing content reader. Each response reopens and verifies
the frozen digest on this serving pod; an open descriptor leases the bytes through streaming.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from central.content_routes import DEB_MEDIA_TYPE, ClientDisconnected, _stream, until_disconnect
from central.content_wiring import ContentServices
from central.db import Database
from central.fleet.bytes import OfferByteReader
from central.fleet.models import (
    BaselineWrite,
    CheckIn,
    FleetError,
    OfferRequest,
    OverrideWrite,
    PolicyWrite,
    RevisionWrite,
)
from central.fleet.service import FleetService
from contracts.time import Clock


def _retry_after(clock: Clock) -> str:
    return str(max(1, 86400 - int(clock.utc()) % 86400))


def mount_fleet_routes(app: FastAPI, *, db: Database, clock: Clock,
                       admin: Callable[..., None],
                       content: ContentServices | None = None) -> FleetService:
    """Add T0 offer/check-in and authenticated F3 policy/status routes.

    `content=None` leaves status and operator policy available in isolated tests, while every
    byte route returns a named 503. The caller mounts this once after additive migration 036.
    """
    service = FleetService(db, clock)
    bytes_reader = OfferByteReader(content.reader if content is not None else None)

    @app.exception_handler(FleetError)
    async def fleet_error(_request: Request, exc: FleetError) -> JSONResponse:
        headers = {"Retry-After": _retry_after(clock)} if exc.status == 429 else None
        return JSONResponse({"error": exc.code}, status_code=exc.status, headers=headers)

    @app.post("/v1/netboot/offers")
    async def create_offer(request: Request, body: OfferRequest) -> Response:
        offer = await asyncio.to_thread(service.create_offer, body)
        try:
            assets = [await asyncio.to_thread(service.offer_asset, UUID(offer["offer_id"]), "base")]
            if offer["initial_app"] is not None:
                assets.append(await asyncio.to_thread(
                    service.offer_asset, UUID(offer["offer_id"]), "app"))
            await until_disconnect(request, bytes_reader.preflight(tuple(assets)))
        except ClientDisconnected:
            return Response(status_code=499)
        return JSONResponse(offer)

    async def offered_bytes(request: Request, offer_id: UUID, kind: str) -> Response:
        asset = await asyncio.to_thread(service.offer_asset, offer_id, kind)
        try:
            opened = await until_disconnect(request, bytes_reader.open_exact(asset))
        except ClientDisconnected:
            return Response(status_code=499)
        try:
            return _stream(opened, DEB_MEDIA_TYPE if kind == "app" else
                           "application/octet-stream")
        except BaseException:
            os.close(opened.fd)
            raise

    @app.get("/v1/netboot/offers/{offer_id}/base")
    async def offered_base(request: Request, offer_id: UUID) -> Response:
        return await offered_bytes(request, offer_id, "base")

    @app.get("/v1/netboot/offers/{offer_id}/app")
    async def offered_app(request: Request, offer_id: UUID) -> Response:
        return await offered_bytes(request, offer_id, "app")

    @app.post("/v1/appliance/check-ins")
    async def check_in(body: CheckIn) -> dict:
        return await asyncio.to_thread(service.record_check_in, body)

    @app.get("/v1/operator/fleet", dependencies=[Depends(admin)])
    async def status() -> dict:
        return await asyncio.to_thread(service.status)

    @app.put("/v1/operator/fleet/app-policy", dependencies=[Depends(admin)])
    async def app_policy(body: PolicyWrite) -> dict:
        return await asyncio.to_thread(service.set_app_policy, body)

    @app.put("/v1/operator/fleet/devices/{device_id}/app-override",
             dependencies=[Depends(admin)])
    async def set_override(device_id: str, body: OverrideWrite) -> dict:
        return await asyncio.to_thread(service.set_override, device_id,
                                       expected_revision=body.expected_revision,
                                       target=body.target)

    @app.delete("/v1/operator/fleet/devices/{device_id}/app-override",
                dependencies=[Depends(admin)])
    async def clear_override(device_id: str, body: RevisionWrite) -> dict:
        return await asyncio.to_thread(service.set_override, device_id,
                                       expected_revision=body.expected_revision, target=None)

    @app.put("/v1/operator/fleet/base-baseline", dependencies=[Depends(admin)])
    async def base_baseline(body: BaselineWrite) -> dict:
        return await asyncio.to_thread(service.set_base_baseline, body)

    return service
