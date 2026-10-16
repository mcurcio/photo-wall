"""The paper profiles routes (a composition-root module like display_routes.py). One route per
question:

| Method | Path | Body | Answer | Refusals |
|---|---|---|---|---|
| GET | /v1/operator/luts | | PaperProfilesView | |
| POST | /v1/operator/luts | the paper profile (.icc bytes) | 201 PaperProfileView; 200 when already listed | 413 too_large, 422 not_a_colour_profile, not_a_printer_profile |
| DELETE | /v1/operator/luts/{profile_sha256} | | 204 | 404 unknown_paper_profile, 409 named_by_a_frame (+ "frames": the Frames' names) |
| GET | /v1/luts/{profile_sha256}/{recipe_id} | | the baked LUT (contracts/lut.py) | 401 unauthorized, 404 unknown_paper_profile |

The operator routes take the `admin` dependency; the LUT itself the Player's bearer (`player`), as
media variants do, and is named by its inputs (contracts/lut.py `LutRef.path`): baked from the
listed profile on every read (in a worker thread) and served with its `Repr-Digest` and
`Content-Length`, which the Player checks the body against. A refusal answers {"error": code}.
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Final

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from central.content_routes import repr_digest
from central.frame_lighting.paper_profiles import (
    LUT_PROFILE_MAX_BYTES,
    LUT_RECIPE_ID,
    PaperProfileRefusal,
    PaperProfileRefused,
    baked_lut,
)
from central.frame_lighting.views import PaperProfilesView, PaperProfileView
from central.infra.lut_store import PgLuts
from central.operator_auth import OPERATOR_PREFIX
from central.request_body import capped_body
from contracts.lut import LUT_BYTES, LUT_MEDIA_TYPE

LUTS: Final = OPERATOR_PREFIX + "luts"


def mount_lut_routes(app: FastAPI, *, admin: Any, player: Any, luts: PgLuts) -> None:
    """Declare the routes above on `app`; PaperProfileRefused becomes {"error": code} at its status,
    with the Frames whose paper names the profile for named_by_a_frame."""

    @app.exception_handler(PaperProfileRefused)
    async def refused(request: Request, exc: PaperProfileRefused) -> JSONResponse:
        body: dict[str, Any] = {"error": exc.code.value}
        if exc.code is PaperProfileRefusal.NAMED_BY_A_FRAME:
            body["frames"] = list(exc.frames)
        return JSONResponse(body, status_code=exc.status)

    @app.get(LUTS, dependencies=[Depends(admin)], response_model=PaperProfilesView)
    def list_luts() -> PaperProfilesView:
        return luts.list_luts()

    @app.post(LUTS, dependencies=[Depends(admin)], response_model=PaperProfileView)
    async def add_lut(request: Request) -> JSONResponse:
        view, created = await asyncio.to_thread(luts.add_lut, await _profile(request))
        return JSONResponse(view.model_dump(mode="json"), status_code=201 if created else 200)

    @app.delete(LUTS + "/{profile_sha256}", dependencies=[Depends(admin)], status_code=204, response_class=Response)
    def remove_lut(profile_sha256: str) -> Response:
        luts.remove_lut(profile_sha256)
        return Response(status_code=204)

    @app.get("/v1/luts/{profile_sha256}/{recipe_id}", dependencies=[Depends(player)])
    async def lut(profile_sha256: str, recipe_id: str) -> Response:
        profile = None if recipe_id != LUT_RECIPE_ID else await asyncio.to_thread(luts.profile, profile_sha256)
        if profile is None:
            raise PaperProfileRefused(PaperProfileRefusal.UNKNOWN_PAPER_PROFILE)
        body = await asyncio.to_thread(baked_lut, profile)
        return Response(body, media_type=LUT_MEDIA_TYPE, headers={
            "Content-Length": str(LUT_BYTES), "Repr-Digest": repr_digest(hashlib.sha256(body).hexdigest()),
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


async def _profile(request: Request) -> bytes:
    """The uploaded profile's bytes; too_large 413 past LUT_PROFILE_MAX_BYTES (central/request_body.py)."""
    return await capped_body(request, LUT_PROFILE_MAX_BYTES, lambda: PaperProfileRefused(PaperProfileRefusal.TOO_LARGE))


__all__ = ["LUTS", "mount_lut_routes"]
