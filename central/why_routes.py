"""The operator's Why routes (roadmap 2b E6; Power and the week: slice T the Frame's Why, slice S3
the week; a composition-root module like display_routes.py). One route per question:

| Method | Path | Body | Answer | Refusals |
|---|---|---|---|---|
| GET | /v1/operator/frames/{frame_id}/why | | FrameWhy | 404 unknown_frame |
| GET | /v1/operator/week?first_day=YYYY-MM-DD | | Week | 422 (FastAPI) for a malformed date |

Every route takes the operator `admin` dependency. WhyRefused answers {"error": code} at its status.
"""
from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from central.why.model import FrameWhy
from central.why.ports import WhyQueries, WhyRefused
from contracts.models import Identifier


def mount_why_routes(app: FastAPI, *, admin: Any, queries: WhyQueries) -> None:
    """Declare the routes above on `app`; WhyRefused becomes {"error": code} at its status."""

    @app.exception_handler(WhyRefused)
    async def refused(request: Request, exc: WhyRefused) -> JSONResponse:
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    @app.get("/v1/operator/frames/{frame_id}/why", dependencies=[Depends(admin)], response_model=FrameWhy)
    def frame_why(frame_id: Identifier) -> FrameWhy:
        return queries.frame_why(frame_id)


__all__ = ["mount_why_routes"]
