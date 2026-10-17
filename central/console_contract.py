"""The console's contract with Central, exported for code generation: the one place the console's
types, limits, patterns and refusal codes come from (scripts/console_types.py turns it into
central/console/src/generated/ at build time; its --check test fails when they drift).

`console_contract()` answers one JSON object:

    {"schemas": {<name>: <JSON Schema>, ...},      # pydantic's, mode "validation" for a body the
                                                    # console sends, "serialization" for a view it reads
                                                    # and for a named alias (CONSOLE_ALIASES)
     "constants": {<NAME>: <number | string>, ...}, # values the console shows or steps by
     "refusals": {<Name>Refusal: [<code>, ...], ...}}  # each context's closed refusal vocabulary,
                                                     # under its Python enum's own name

Keys are sorted and every value is plain JSON, so the generated files are stable for an unchanged
contract. A model, constant or refusal vocabulary the console needs is added to the tables below,
never retyped in TypeScript.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, TypeAdapter

from central.content_catalog.views import ReleasesView, VersionChoice
from central.displays.views import LightView
from central.fleet.models import TrialRefusal
from central.fleet.node_calibration import TrialBegin, TrialEdit, TrialStep
from central.fleet.position_history import PositionRestoreRefusal
from central.fleet.position_views import PositionHistoryView, PositionRestore
from central.frame_lighting.model import FrameLightingRefusal
from central.frame_lighting.paper_profiles import PaperProfileRefusal
from central.frame_lighting.views import PaperProfilesView, PaperProfileView
from central.infra.home_assistant import references
from central.infra.home_assistant.views import HomeAssistantStatesView
from central.infra.light_sensor_judge import PI_SOURCE
from central.room_light.model import (
    ROOM_LIGHT_NAME_MAX,
    SENSORS_MAX,
    DrawnReportRefusal,
    ReportRefusal,
    RoomLight,
    RoomLightRefusal,
    Sensor,
)
from central.room_light.views import DrawnView, RefusedReportsView, RoomLightsView
from contracts.central_api import InstallRefusal, InstallStarted
from contracts.frame_lighting import (
    BEAM_DEGREES_MAX,
    CLOSE_REACH_MM,
    FRAME_LIGHTS_MAX,
    GLOSS_INK,
    INK_DENSITY_MAX,
    INK_DENSITY_MIN,
    MAT_DEPTH_MM_MAX,
    MATTE_INK,
    SHEET_WHITENESS,
    WHITENESS_MIN,
    FrameLighting,
)
from contracts.kelvin import KELVIN_TABLE_MAX, KELVIN_TABLE_MIN, KELVIN_TABLE_STEP
from contracts.node_picture import (
    MATCHED_LUX_MAX,
    MATCHED_LUX_MIN,
    PANEL_BLACK_MAX,
    PANEL_BLACK_MIN,
)
from contracts.room_light import (
    LUX_MAX,
    ROOM_LIGHT_ID_PATTERN,
    ROOM_LIGHTS_MAX,
    LightWord,
    Lux,
    Reading,
)

CONSOLE_BODIES: Final[tuple[type[BaseModel], ...]] = (   # bodies the console sends (and RoomLight, a union)
    Sensor, TrialBegin, TrialEdit, TrialStep, FrameLighting, PositionRestore, VersionChoice)
CONSOLE_VIEWS: Final[tuple[type[BaseModel], ...]] = (    # answers the console reads
    RoomLightsView, DrawnView, RefusedReportsView, PaperProfileView, PaperProfilesView,
    PositionHistoryView, LightView, HomeAssistantStatesView, ReleasesView, InstallStarted)
CONSOLE_ALIASES: Final[Mapping[str, object]] = {     # named types a view's fields take, by their name
    "Lux": Lux, "LightWord": LightWord, "Reading": Reading}
CONSOLE_CONSTANTS: Final[Mapping[str, float | int | str]] = {
    "FRAME_LIGHTS_MAX": FRAME_LIGHTS_MAX, "MAT_DEPTH_MM_MAX": MAT_DEPTH_MM_MAX, "CLOSE_REACH_MM": CLOSE_REACH_MM,
    "BEAM_DEGREES_MAX": BEAM_DEGREES_MAX, "WHITENESS_MIN": WHITENESS_MIN, "INK_DENSITY_MIN": INK_DENSITY_MIN,
    "INK_DENSITY_MAX": INK_DENSITY_MAX, "GLOSS_INK": GLOSS_INK, "MATTE_INK": MATTE_INK,
    "SHEET_WHITENESS": SHEET_WHITENESS, "LUX_MAX": LUX_MAX, "KELVIN_TABLE_MIN": KELVIN_TABLE_MIN,
    "KELVIN_TABLE_MAX": KELVIN_TABLE_MAX, "KELVIN_TABLE_STEP": KELVIN_TABLE_STEP, "ROOM_LIGHTS_MAX": ROOM_LIGHTS_MAX,
    "SENSORS_MAX": SENSORS_MAX, "ROOM_LIGHT_NAME_MAX": ROOM_LIGHT_NAME_MAX,
    "ROOM_LIGHT_ID_PATTERN": ROOM_LIGHT_ID_PATTERN, "HOME_ASSISTANT_SOURCE": references.SOURCE, "PI_SOURCE": PI_SOURCE,
    "MATCHED_LUX_MIN": MATCHED_LUX_MIN, "MATCHED_LUX_MAX": MATCHED_LUX_MAX, "PANEL_BLACK_MIN": PANEL_BLACK_MIN,
    "PANEL_BLACK_MAX": PANEL_BLACK_MAX,
}
CONSOLE_REFUSALS: Final[tuple[type[StrEnum], ...]] = (   # each context's closed refusal enum
    RoomLightRefusal, FrameLightingRefusal, PaperProfileRefusal, ReportRefusal, DrawnReportRefusal, TrialRefusal,
    PositionRestoreRefusal, InstallRefusal,
)


def console_contract() -> dict[str, object]:
    """The contract above as one JSON-able object (deterministic for unchanged tables)."""
    schemas: dict[str, object] = {model.__name__: model.model_json_schema(mode="validation")
                                  for model in CONSOLE_BODIES}
    schemas["RoomLight"] = TypeAdapter(RoomLight).json_schema(mode="validation")
    schemas.update({name: TypeAdapter(alias).json_schema(mode="serialization")
                    for name, alias in CONSOLE_ALIASES.items()})
    schemas.update({model.__name__: model.model_json_schema(mode="serialization") for model in CONSOLE_VIEWS})
    contract = {"schemas": schemas, "constants": dict(CONSOLE_CONSTANTS),
                "refusals": {refusal.__name__: [code.value for code in refusal] for refusal in CONSOLE_REFUSALS}}
    return json.loads(json.dumps(contract, sort_keys=True))


__all__ = ["CONSOLE_ALIASES", "CONSOLE_BODIES", "CONSOLE_CONSTANTS", "CONSOLE_REFUSALS", "CONSOLE_VIEWS", "console_contract"]
