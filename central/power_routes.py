"""The operator's power routes (roadmap 2a; Power and the week: slice T serves GET night-off,
slice S2 the rest; a composition-root module like display_routes.py). One route per question:

| Method | Path | Body | Answer | Refusals |
|---|---|---|---|---|
| GET | /v1/operator/power/night-off | | NightOffView | |
| PUT | /v1/operator/power/night-off | NightOffSave | NightOffView | 409 changed, 422 no-days, needs-place, unknown-frame |
| GET | /v1/operator/power/holds | | tuple[HoldView, ...] | |
| GET | /v1/operator/power/hold-lasts | | HoldLastsView | |
| PUT | /v1/operator/power/hold-lasts | HoldLastsView | HoldLastsView | |
| POST | /v1/operator/power/all-off | HoldRequest | 201 tuple[HoldView, ...] | |
| POST | /v1/operator/power/all-on | HoldRequest | 201 tuple[HoldView, ...] | |
| POST | /v1/operator/power/resume | ResumeRequest | 204 | |

Every route takes the operator `admin` dependency. PowerRefused answers {"error": code} at its
status, the shape apiWrite.js reads. 1b's per-Frame Test routes stay in display_routes.py.
"""
from __future__ import annotations

from typing import Any, Final

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from central.operator_auth import OPERATOR_PREFIX
from central.power.ports import PowerCommands, PowerQueries, PowerRefused
from central.power.views import (
    HoldLastsView,
    HoldRequest,
    HoldView,
    NightOffSave,
    NightOffView,
    ResumeRequest,
)

POWER: Final = OPERATOR_PREFIX + "power/"


def mount_power_routes(app: FastAPI, *, admin: Any, queries: PowerQueries, commands: PowerCommands) -> None:
    """Declare the routes above on `app`; PowerRefused becomes {"error": code} at its status."""

    @app.exception_handler(PowerRefused)
    async def refused(request: Request, exc: PowerRefused) -> JSONResponse:
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    @app.get(POWER + "night-off", dependencies=[Depends(admin)], response_model=NightOffView)
    def night_off() -> NightOffView:
        return queries.night_off()

    @app.put(POWER + "night-off", dependencies=[Depends(admin)], response_model=NightOffView)
    def save_night_off(save: NightOffSave) -> NightOffView:
        return commands.save_night_off(save)

    @app.get(POWER + "holds", dependencies=[Depends(admin)], response_model=tuple[HoldView, ...])
    def holds() -> tuple[HoldView, ...]:
        return queries.holds()

    @app.get(POWER + "hold-lasts", dependencies=[Depends(admin)], response_model=HoldLastsView)
    def hold_lasts() -> HoldLastsView:
        return queries.hold_lasts()

    @app.put(POWER + "hold-lasts", dependencies=[Depends(admin)], response_model=HoldLastsView)
    def set_hold_lasts(setting: HoldLastsView) -> HoldLastsView:
        return commands.set_hold_lasts(setting.hold_lasts)

    @app.post(POWER + "all-off", dependencies=[Depends(admin)], status_code=201, response_model=tuple[HoldView, ...])
    def all_off(request: HoldRequest) -> tuple[HoldView, ...]:
        return commands.all_off(request.request_id)

    @app.post(POWER + "all-on", dependencies=[Depends(admin)], status_code=201, response_model=tuple[HoldView, ...])
    def all_on(request: HoldRequest) -> tuple[HoldView, ...]:
        return commands.all_on(request.request_id)

    @app.post(POWER + "resume", dependencies=[Depends(admin)], status_code=204, response_class=Response)
    def resume(request: ResumeRequest) -> Response:
        commands.resume_schedule(request.frame_ids)
        return Response(status_code=204)


__all__ = ["mount_power_routes"]
