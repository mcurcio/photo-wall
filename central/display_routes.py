"""The operator's Display and power routes (roadmap 1b; run ledger .claude/runs/display-1b.md,
slices C1-C3; a composition-root module like library_routes.py). One route per question:

| Method | Path | Body | Answer | Refusals |
|---|---|---|---|---|
| GET | /v1/operator/frames/{frame_id}/display | | FrameDisplayView | 404 unknown_frame |
| GET | /v1/operator/frames/{frame_id}/power | | FramePowerView | 404 unknown_frame |
| POST | /v1/operator/frames/{frame_id}/power-tests | PowerTestRequest | 202 PowerTestAccepted | 404 unknown_frame, 409 frame_unbound |
| PUT | /v1/operator/displays/{display_id}/power-settings | DisplayPowerSettings | DisplayPowerSettings | 404 unknown_display |

Every route takes the operator `admin` dependency, as every /v1/operator route does. A refusal
answers {"error": code} with its status, the shape apiWrite.js reads. create_app mounts them through
`mount_display_routes`: slice C1 mounts GET display; the other three are declared in
`_mount_power_routes`, which slice C3 implements and calls from `mount_display_routes`.
"""
from __future__ import annotations

from typing import Any, Final
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from central.displays.ports import DisplayCommands, DisplayQueries, DisplayRefused
from central.displays.views import (
    DisplayPowerSettings,
    FrameDisplayView,
    FramePowerView,
    PowerTestAccepted,
    PowerTestRequest,
)
from central.operator_auth import OPERATOR_PREFIX
from contracts.models import Identifier

FRAMES: Final = OPERATOR_PREFIX + "frames/"
DISPLAYS: Final = OPERATOR_PREFIX + "displays/"


def mount_display_routes(app: FastAPI, *, admin: Any, queries: DisplayQueries, commands: DisplayCommands) -> None:
    """Declare the routes above on `app`; DisplayRefused becomes {"error": code} at its status."""

    @app.exception_handler(DisplayRefused)
    async def refused(request: Request, exc: DisplayRefused) -> JSONResponse:
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    @app.get(FRAMES + "{frame_id}/display", dependencies=[Depends(admin)], response_model=FrameDisplayView)
    def frame_display(frame_id: Identifier) -> FrameDisplayView:
        return queries.frame_display(frame_id)


def _mount_power_routes(app: FastAPI, *, admin: Any, queries: DisplayQueries, commands: DisplayCommands) -> None:
    """The Power tab's three routes: slice C3 implements them and mounts them from
    `mount_display_routes`."""

    @app.get(FRAMES + "{frame_id}/power", dependencies=[Depends(admin)], response_model=FramePowerView)
    def frame_power(frame_id: Identifier) -> FramePowerView:
        raise NotImplementedError

    @app.post(FRAMES + "{frame_id}/power-tests", dependencies=[Depends(admin)], status_code=202,
              response_model=PowerTestAccepted)
    def power_test(frame_id: Identifier, request: PowerTestRequest) -> PowerTestAccepted:
        raise NotImplementedError

    @app.put(DISPLAYS + "{display_id}/power-settings", dependencies=[Depends(admin)],
             response_model=DisplayPowerSettings)
    def power_settings(display_id: UUID, request: DisplayPowerSettings) -> DisplayPowerSettings:
        raise NotImplementedError
