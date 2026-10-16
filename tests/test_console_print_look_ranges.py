"""The console's lighting and Display light controls take exactly the contracts' ranges (decision
0028; print look review finding: ranges copied into the console with nothing tying them to their
home).

The homes are contracts/frame_lighting.py (`FrameLighting`, its lights and `Paper`, the presets,
FRAME_LIGHTS_MAX) and contracts/node_picture.py (MATCHED_LUX_MIN..MAX, PANEL_BLACK_MIN..MAX, the
software group's ranges); the console's copies are `RANGES`, `PRESETS` and SHEET_WHITENESS in
domain/frame-lighting.tsx and MATCHED_LUX, PANEL_BLACK_PERCENT and the Sliders' literal bounds in
domain/frame-picture.tsx, read here from the source as written.

Mutation probes: INK_DENSITY_MAX read as 3.0 in RANGES (it is 5.0), lights max 5, Gloss 2.2, Matched
light max 2000, Panel black min 0.01 % -> this test names the value that drifted.
"""
from __future__ import annotations

import re
from pathlib import Path

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
    CloseLight,
    FarLight,
    FrameLighting,
)
from contracts.node_picture import (
    GAMMA_TENTHS_MAX,
    GAMMA_TENTHS_MIN,
    KELVIN_MAX,
    KELVIN_MIN,
    KELVIN_STEP,
    MATCHED_LUX_MAX,
    MATCHED_LUX_MIN,
    PANEL_BLACK_MAX,
    PANEL_BLACK_MIN,
    SOFTWARE_PERCENT_MAX,
)
from contracts.room_light import LUX_MAX

DOMAIN = Path(__file__).parents[1] / "central/console/src/domain"
SLIDER = re.compile(r'<Field label="([^"]+)"[^>]*>\s*<Slider min=\{([\d.]+)\} max=\{([\d.]+)\} step=\{([\d.]+)\}')
NUMBER = r"-?[\d.]+"


def _ranges() -> dict[str, tuple[float, float, float]]:
    """frame-lighting.tsx's RANGES block, as written: name -> (min, max, step)."""
    source = (DOMAIN / "frame-lighting.tsx").read_text()
    block = re.search(r"export const RANGES = \{(.*?)\} as const;", source, re.S)
    assert block, "RANGES is gone from frame-lighting.tsx"
    return {name: (float(low), float(high), float(step)) for name, low, high, step in
            re.findall(rf"(\w+): \[({NUMBER}), ({NUMBER}), ({NUMBER})\]", block.group(1))}


def _bounds(model, field: str) -> tuple[float, float]:
    schema = model.model_json_schema()["properties"][field]
    low = schema.get("minimum", schema.get("exclusiveMinimum"))
    high = schema.get("maximum", schema.get("exclusiveMaximum"))
    return float(low), float(high)


def _paper(field: str) -> tuple[float, float]:
    schema = FrameLighting.model_json_schema()["$defs"]["Paper"]["properties"][field]
    return float(schema["minimum"]), float(schema["maximum"])


def test_the_lighting_editor_takes_the_frame_lightings_ranges():
    ranges = _ranges()
    depth = FrameLighting.model_json_schema()["properties"]["mat_depth_mm"]
    lows_and_highs = {name: value[:2] for name, value in ranges.items()}
    assert lows_and_highs == {
        "mat_depth_mm": (depth["minimum"], MAT_DEPTH_MM_MAX),
        "lights": (0.0, FRAME_LIGHTS_MAX),
        "lux": (0.0, LUX_MAX),
        "from_degrees": _bounds(FarLight, "from_degrees"),
        "steep_degrees": _bounds(FarLight, "steep_degrees"),
        "softness_degrees": _bounds(FarLight, "softness_degrees"),
        "across_mm": (-CLOSE_REACH_MM, CLOSE_REACH_MM),
        "up_mm": (-CLOSE_REACH_MM, CLOSE_REACH_MM),
        "out_mm": _bounds(CloseLight, "out_mm"),
        "beam_degrees": (0.0, BEAM_DEGREES_MAX),
        "whiteness": _paper("whiteness"),
        "ink_density": _paper("ink_density"),
    }, lows_and_highs
    assert (_paper("whiteness"), _paper("ink_density")) == ((WHITENESS_MIN, 1), (INK_DENSITY_MIN, INK_DENSITY_MAX))
    assert ranges["mat_depth_mm"][2] == depth["multipleOf"]
    assert _bounds(CloseLight, "outer_beam_degrees")[1] == BEAM_DEGREES_MAX


def test_the_presets_and_the_sheet_are_the_contracts():
    source = (DOMAIN / "frame-lighting.tsx").read_text()
    presets = re.search(rf"export const PRESETS = \{{ gloss: ({NUMBER}), matte: ({NUMBER}) \}} as const;", source)
    assert presets and (float(presets[1]), float(presets[2])) == (GLOSS_INK, MATTE_INK), "PRESETS drifted"
    sheet = re.search(rf"^export const SHEET_WHITENESS = ({NUMBER});$", source, re.M)
    assert sheet and float(sheet[1]) == SHEET_WHITENESS, "SHEET_WHITENESS drifted"


def test_the_picture_tabs_light_and_sliders_take_the_contracts_ranges():
    source = (DOMAIN / "frame-picture.tsx").read_text()
    matched = re.search(rf"export const MATCHED_LUX = \[({NUMBER}), ({NUMBER})\] as const;", source)
    black = re.search(rf"export const PANEL_BLACK_PERCENT = \[({NUMBER}), ({NUMBER})\] as const;", source)
    assert matched and (float(matched[1]), float(matched[2])) == (MATCHED_LUX_MIN, MATCHED_LUX_MAX), "Matched light drifted"
    assert black and (float(black[1]) / 100, float(black[2]) / 100) == (PANEL_BLACK_MIN, PANEL_BLACK_MAX), "Panel black drifted"
    sliders = {label: (float(low), float(high), float(step)) for label, low, high, step in SLIDER.findall(source)}
    assert sliders == {
        "Contrast": (0.0, SOFTWARE_PERCENT_MAX, 1.0),
        "Colour temperature": (KELVIN_MIN, KELVIN_MAX, KELVIN_STEP),
        "Gamma": (GAMMA_TENTHS_MIN / 10, GAMMA_TENTHS_MAX / 10, 0.1),
    }, sliders
