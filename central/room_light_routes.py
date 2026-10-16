"""House › Lights' routes and the Player's drawn report (a composition-root module like
power_routes.py). One route per question, the shapes central/room_light/views.py's:

| Method | Path | Body | Answer | Refusals |
|---|---|---|---|---|
| GET | /v1/operator/house/lights | | RoomLightsView | |
| PUT | /v1/operator/house/lights/{light_id} | RoomLight | RoomLightView | 404 unknown_sensor; 422 unknown_address; 409 reference_taken, sensor_reports_a_light, named_by_a_frame, too_many |
| DELETE | /v1/operator/house/lights/{light_id} | | 204 | 404 unknown_room_light; 409 named_by_a_frame |
| PUT | /v1/operator/house/sensors/{sensor_id} | Sensor | SensorView | 422 unknown_address; 409 reference_taken, too_many |
| DELETE | /v1/operator/house/sensors/{sensor_id} | | 204 | 404 unknown_sensor; 409 sensor_reports_a_light |
| GET | /v1/operator/house/lights/drawn | | DrawnView | |
| GET | /v1/operator/house/refused-reports | | RefusedReportsView | |
| PUT | ROOM_LIGHTS_DRAWN_PATH (the Player's bearer) | RoomLightsDrawn | 204 | 413 room_lights_drawn_too_large, 422 room_lights_drawn_invalid |

The operator routes take the `admin` dependency. A refusal answers {"error": <code>} at the one
status `REFUSAL_STATUS` gives its code (one exception handler); an id or body outside its shape
answers 422.
"""
from __future__ import annotations

import asyncio
from typing import Annotated, Any, Final

from fastapi import Depends, FastAPI, Path, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from central.operator_auth import OPERATOR_PREFIX
from central.request_body import capped_body
from central.room_light.model import (
    DrawnReportRefusal,
    ReportedLight,
    RoomLight,
    RoomLightRefusal,
    RoomLightRefused,
    Sensor,
)
from central.room_light.ports import DrawnReports, ReportSources, RoomLightSetup
from central.room_light.views import (
    DrawnView,
    RefusedReportsView,
    RoomLightsView,
    RoomLightView,
    SensorView,
)
from contracts.room_light import (
    ROOM_LIGHT_ID_PATTERN,
    ROOM_LIGHTS_DRAWN_BYTES,
    ROOM_LIGHTS_DRAWN_PATH,
    RoomLightsDrawn,
)

HOUSE_LIGHTS: Final = OPERATOR_PREFIX + "house/lights"
HOUSE_SENSORS: Final = OPERATOR_PREFIX + "house/sensors"
HOUSE_REFUSED: Final = OPERATOR_PREFIX + "house/refused-reports"
_ID: Final = rf"^{ROOM_LIGHT_ID_PATTERN}$"
LightId = Annotated[str, Path(pattern=_ID)]
REFUSAL_STATUS: Final[dict[RoomLightRefusal | DrawnReportRefusal, int]] = {
    RoomLightRefusal.UNKNOWN_ROOM_LIGHT: 404,
    RoomLightRefusal.UNKNOWN_SENSOR: 404,
    RoomLightRefusal.UNKNOWN_ADDRESS: 422,
    RoomLightRefusal.REFERENCE_TAKEN: 409,
    RoomLightRefusal.SENSOR_REPORTS_A_LIGHT: 409,
    RoomLightRefusal.NAMED_BY_A_FRAME: 409,
    RoomLightRefusal.TOO_MANY: 409,
    DrawnReportRefusal.TOO_LARGE: 413,
    DrawnReportRefusal.INVALID: 422,
}
SensorIdPath = Annotated[str, Path(pattern=_ID)]


def mount_room_light_routes(app: FastAPI, *, admin: Any, player: Any, lights: RoomLightSetup,
                            sources: ReportSources, drawn: DrawnReports) -> None:
    """Declare the routes above on `app`. `player` answers the Player's identity ({"id": ...})."""

    @app.exception_handler(RoomLightRefused)
    async def refused(_request: Request, exc: RoomLightRefused) -> JSONResponse:
        return _refusal(exc.code)

    def known(source: str, address: str) -> None:
        refusal = sources.address_refusal(source, address)
        if refusal is not None:
            raise RoomLightRefused(refusal)

    @app.get(HOUSE_LIGHTS, dependencies=[Depends(admin)], response_model=RoomLightsView)
    def room_lights() -> RoomLightsView:
        return lights.lights()

    @app.get(HOUSE_LIGHTS + "/drawn", dependencies=[Depends(admin)], response_model=DrawnView)
    def drawn_by_players() -> DrawnView:
        return lights.drawn()

    @app.get(HOUSE_REFUSED, dependencies=[Depends(admin)], response_model=RefusedReportsView)
    def refused_reports() -> RefusedReportsView:
        return lights.refused()

    @app.put(HOUSE_LIGHTS + "/{light_id}", dependencies=[Depends(admin)], response_model=RoomLightView)
    def save_room_light(light_id: LightId, light: RoomLight) -> RoomLightView:
        if isinstance(light, ReportedLight):
            known(light.reporter.source, light.reporter.address)
        return lights.save_room_light(light_id, light)

    @app.delete(HOUSE_LIGHTS + "/{light_id}", dependencies=[Depends(admin)], status_code=204,
                response_class=Response)
    def remove_room_light(light_id: LightId) -> Response:
        lights.remove_room_light(light_id)
        return Response(status_code=204)

    @app.put(HOUSE_SENSORS + "/{sensor_id}", dependencies=[Depends(admin)], response_model=SensorView)
    def save_sensor(sensor_id: SensorIdPath, sensor: Sensor) -> SensorView:
        known(sensor.at.source, sensor.at.address)
        return lights.save_sensor(sensor_id, sensor)

    @app.delete(HOUSE_SENSORS + "/{sensor_id}", dependencies=[Depends(admin)], status_code=204,
                response_class=Response)
    def remove_sensor(sensor_id: SensorIdPath) -> Response:
        lights.remove_sensor(sensor_id)
        return Response(status_code=204)

    @app.put(ROOM_LIGHTS_DRAWN_PATH, status_code=204, response_class=Response)
    async def drawn_report(request: Request, identity: dict = Depends(player)) -> Response:  # noqa: B008
        try:
            raw = await capped_body(request, ROOM_LIGHTS_DRAWN_BYTES, _TooLarge)
        except _TooLarge:
            return _refusal(DrawnReportRefusal.TOO_LARGE)
        try:
            report = RoomLightsDrawn.model_validate_json(raw, strict=True)
        except ValidationError:
            return _refusal(DrawnReportRefusal.INVALID)
        await asyncio.to_thread(drawn.record_drawn, identity["id"], report)
        return Response(status_code=204)


def _refusal(code: RoomLightRefusal | DrawnReportRefusal) -> JSONResponse:
    return JSONResponse({"error": code.value}, status_code=REFUSAL_STATUS[code])


class _TooLarge(Exception):
    """The drawn report's body passed ROOM_LIGHTS_DRAWN_BYTES."""


__all__ = ["HOUSE_LIGHTS", "HOUSE_REFUSED", "HOUSE_SENSORS", "REFUSAL_STATUS", "mount_room_light_routes"]
