"""House > Lights' operator routes (decision 0028; a composition-root module like power_routes.py).
One route per question, the shapes central/room_light/views.py's:

| Method | Path | Body | Answer | Refusals |
|---|---|---|---|---|
| GET | /v1/operator/house/lights | | RoomLightsView | |
| PUT | /v1/operator/house/lights/{light_id} | RoomLightSave | RoomLightView | 409 SetupRefusal |
| DELETE | /v1/operator/house/lights/{light_id} | | 204 | 409 SetupRefusal |
| PUT | /v1/operator/house/sensors/{sensor_id} | SensorSave | SensorView | 409 SetupRefusal |
| DELETE | /v1/operator/house/sensors/{sensor_id} | | 204 | 409 SetupRefusal |

Every route takes the operator `admin` dependency. A refusal answers {"error": <SetupRefusal value>}
at 409, the shape apiWrite.js reads; an id or body outside its shape answers 422.
"""
from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import Depends, FastAPI, Path
from fastapi.responses import JSONResponse, Response

from central.operator_auth import OPERATOR_PREFIX
from central.room_light.model import RoomLight, Sensor
from central.room_light.ports import RoomLightSetup
from central.room_light.views import (
    RoomLightSave,
    RoomLightsView,
    RoomLightView,
    SensorSave,
    SensorView,
)
from contracts.room_light import ROOM_LIGHT_ID_PATTERN

HOUSE_LIGHTS: Final = OPERATOR_PREFIX + "house/lights"
HOUSE_SENSORS: Final = OPERATOR_PREFIX + "house/sensors"
_ID: Final = rf"^{ROOM_LIGHT_ID_PATTERN}$"
LightId = Annotated[str, Path(pattern=_ID)]
SensorIdPath = Annotated[str, Path(pattern=_ID)]


def _refused(code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=409)


def mount_room_light_routes(app: FastAPI, *, admin: Any, lights: RoomLightSetup) -> None:
    """Declare the routes above on `app`."""

    @app.get(HOUSE_LIGHTS, dependencies=[Depends(admin)], response_model=RoomLightsView)
    def room_lights() -> RoomLightsView:
        return lights.lights()

    @app.put(HOUSE_LIGHTS + "/{light_id}", dependencies=[Depends(admin)], response_model=RoomLightView)
    def save_room_light(light_id: LightId, save: RoomLightSave) -> Any:
        refusal = lights.save_room_light(RoomLight(id=light_id, name=save.name, reporter=save.reporter,
                                                   colour=save.colour))
        if refusal is not None:
            return _refused(refusal.value)
        return next(view for view in lights.lights().lights if view.id == light_id)

    @app.delete(HOUSE_LIGHTS + "/{light_id}", dependencies=[Depends(admin)], status_code=204,
                response_class=Response)
    def remove_room_light(light_id: LightId) -> Response:
        refusal = lights.remove_room_light(light_id)
        return Response(status_code=204) if refusal is None else _refused(refusal.value)

    @app.put(HOUSE_SENSORS + "/{sensor_id}", dependencies=[Depends(admin)], response_model=SensorView)
    def save_sensor(sensor_id: SensorIdPath, save: SensorSave) -> Any:
        refusal = lights.save_sensor(Sensor(id=sensor_id, name=save.name, at=save.at))
        if refusal is not None:
            return _refused(refusal.value)
        return next(view for view in lights.lights().sensors if view.id == sensor_id)

    @app.delete(HOUSE_SENSORS + "/{sensor_id}", dependencies=[Depends(admin)], status_code=204,
                response_class=Response)
    def remove_sensor(sensor_id: SensorIdPath) -> Response:
        refusal = lights.remove_sensor(sensor_id)
        return Response(status_code=204) if refusal is None else _refused(refusal.value)


__all__ = ["HOUSE_LIGHTS", "HOUSE_SENSORS", "mount_room_light_routes"]
