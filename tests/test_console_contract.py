"""The console's contract with Central (central/console_contract.py): one JSON object of schemas,
constants and refusal vocabularies, stable for unchanged tables, holding Central's own values."""
from __future__ import annotations

import json

from central.console_contract import console_contract
from central.frame_lighting.model import FrameLightingRefusal
from central.infra.home_assistant.topics import SOURCE
from central.room_light.model import RoomLightRefusal
from contracts.frame_lighting import FRAME_LIGHTS_MAX, SHEET_WHITENESS
from contracts.room_light import ROOM_LIGHT_ID_PATTERN


def test_the_contract_is_plain_sorted_json_and_stable():
    first, second = console_contract(), console_contract()
    assert first == second
    text = json.dumps(first)
    assert json.dumps(json.loads(text), sort_keys=True) == text
    assert sorted(first) == ["constants", "refusals", "schemas"]


def test_the_contract_carries_centrals_own_values():
    contract = console_contract()
    constants = contract["constants"]
    assert (constants["FRAME_LIGHTS_MAX"], constants["SHEET_WHITENESS"], constants["ROOM_LIGHT_ID_PATTERN"],
            constants["HOME_ASSISTANT_SOURCE"], constants["PI_SOURCE"]) == (
        FRAME_LIGHTS_MAX, SHEET_WHITENESS, ROOM_LIGHT_ID_PATTERN, SOURCE, "pi")
    assert contract["refusals"]["room_light"] == [code.value for code in RoomLightRefusal]
    assert contract["refusals"]["frame_lighting"] == [code.value for code in FrameLightingRefusal]
    assert {"RoomLight", "Sensor", "TrialBegin", "TrialEdit", "TrialStep", "FrameLighting", "RoomLightsView",
            "DrawnView", "RefusedReportsView", "HomeAssistantView", "PaperProfileView",
            "PositionHistoryView"} <= set(contract["schemas"])
    kinds = {each["properties"]["kind"]["const"] for each in contract["schemas"]["RoomLight"]["$defs"].values()
             if "kind" in each.get("properties", {}) and each.get("title") in ("ReportedLight", "MeasuredLight", "FixedLight")}
    assert kinds == {"reported", "measured", "fixed"}
