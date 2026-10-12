"""The operator's power routes (roadmap 2a; Power and the week: slice T serves GET night-off,
slice S2 the rest; a composition-root module like display_routes.py). One route per question:

| Method | Path | Body | Answer | Refusals |
|---|---|---|---|---|
| GET | /v1/operator/power/night-off | | NightOffView | |
| PUT | /v1/operator/power/night-off | NightOffSave | NightOffView | 409 changed, 422 no-days, needs-place, unknown-frame |
| GET | /v1/operator/power/holds | | tuple[HoldView, ...] | |
| POST | /v1/operator/power/all-off | HoldRequest | 201 HoldView | |
| POST | /v1/operator/power/all-on | HoldRequest | 201 HoldView | |
| POST | /v1/operator/power/resume | ResumeRequest | 204 | |

Every route takes the operator `admin` dependency. PowerRefused answers {"error": code} at its
status, the shape apiWrite.js reads. 1b's per-Frame Test routes stay in display_routes.py.
"""
from __future__ import annotations

from typing import Any, Final

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from central.operator_auth import OPERATOR_PREFIX
from central.power.ports import PowerCommands, PowerQueries, PowerRefused
from central.power.views import NightOffView

POWER: Final = OPERATOR_PREFIX + "power/"


def mount_power_routes(app: FastAPI, *, admin: Any, queries: PowerQueries, commands: PowerCommands) -> None:
    """Declare the routes above on `app`; PowerRefused becomes {"error": code} at its status."""

    @app.exception_handler(PowerRefused)
    async def refused(request: Request, exc: PowerRefused) -> JSONResponse:
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    @app.get(POWER + "night-off", dependencies=[Depends(admin)], response_model=NightOffView)
    def night_off() -> NightOffView:
        return queries.night_off()


__all__ = ["mount_power_routes"]
