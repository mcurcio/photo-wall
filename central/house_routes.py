"""The operator's Settings › House routes (roadmap 2b A4; Power and the week, slice S3; a
composition-root module like display_routes.py). One route per question:

| Method | Path | Body | Answer | Refusals |
|---|---|---|---|---|
| GET | /v1/operator/house | | HouseSettings | |
| PUT | /v1/operator/house | HouseSettingsDraft | HouseSettings | 422 unknown-zone |

Every route takes the operator `admin` dependency. HouseRefused answers {"error": code} at its
status, the shape apiWrite.js reads.
"""
from __future__ import annotations

from typing import Any, Final

from fastapi import Body, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from central.house_time.model import HouseSettings, HouseSettingsDraft
from central.house_time.ports import HouseCommands, HouseQueries, HouseRefused
from central.operator_auth import OPERATOR_PREFIX

HOUSE: Final = OPERATOR_PREFIX + "house"


def _draft(body: Any) -> HouseSettingsDraft:
    """The body as a draft. A zone the IANA database does not know is HouseRefused("unknown-zone"),
    the code the console reads, rather than FastAPI's generic 422 (HouseTime refuses it at parse)."""
    try:
        return HouseSettingsDraft.model_validate(body)
    except ValidationError as exc:
        if any(error["loc"] == ("time", "zone") and "unknown-zone" in error["msg"] for error in exc.errors()):
            raise HouseRefused("unknown-zone", 422) from None
        raise RequestValidationError(exc.errors(include_url=False)) from None


def mount_house_routes(app: FastAPI, *, admin: Any, queries: HouseQueries, commands: HouseCommands) -> None:
    """Declare the routes above on `app`; HouseRefused becomes {"error": code} at its status."""

    @app.exception_handler(HouseRefused)
    async def refused(request: Request, exc: HouseRefused) -> JSONResponse:
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    @app.get(HOUSE, dependencies=[Depends(admin)], response_model=HouseSettings)
    def house_settings() -> HouseSettings:
        return queries.house_settings()

    @app.put(HOUSE, dependencies=[Depends(admin)], response_model=HouseSettings)
    def save_house_settings(body: Any = Body(...)) -> HouseSettings:
        return commands.save_house_settings(_draft(body))


__all__ = ["mount_house_routes"]
