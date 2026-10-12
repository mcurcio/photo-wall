"""The console's print-look and mat sliders take exactly the contracts' ranges (print look, review
finding: the ranges were copied into the console with nothing tying them to their home).

The homes are contracts/models.py (`Mat`, `Light`, MAT_LIGHTS_MAX) and contracts/node_picture.py
(`SoftwarePicture`'s ranges); the console's copies are the Sliders' literal bounds in
domain/frame-mat.tsx and domain/frame-picture.tsx, read here from the source as written.

Mutation probes: Comes from max 360, Strength max 100 (not tenths shown as 0.0..10.0), LIGHTS_MAX 5,
Saturation 200, Black floor max 100 -> this test names the slider that drifted.
"""
from __future__ import annotations

import re
from pathlib import Path

from contracts.models import MAT_DEPTH_MM_MAX, MAT_LIGHTS_MAX, Light, Mat
from contracts.node_picture import (
    BLACK_FLOOR_TENTHS_MAX,
    GAMMA_TENTHS_MAX,
    GAMMA_TENTHS_MIN,
    KELVIN_MAX,
    KELVIN_MIN,
    KELVIN_STEP,
    LUT_AMOUNT_PERCENT_MAX,
    ROLLOFF_PERCENT_MAX,
    SATURATION_PERCENT_MAX,
    SOFTWARE_PERCENT_MAX,
)

DOMAIN = Path(__file__).parents[1] / "central/console/src/domain"
SLIDER = re.compile(r'<Field label="([^"]+)"[^>]*>\s*<Slider min=\{([\d.]+)\} max=\{([\d.]+)\} step=\{([\d.]+)\}')
PERCENT = re.compile(r'percent\("[^"]+", "([^"]+)", \w+, (\d+),')


def _sliders(name: str) -> dict[str, tuple[float, float, float]]:
    source = (DOMAIN / name).read_text()
    return {label: (float(low), float(high), float(step)) for label, low, high, step in SLIDER.findall(source)}


def _bounds(model, field: str) -> tuple[float, float]:
    schema = model.model_json_schema()["properties"][field]
    return float(schema["minimum"]), float(schema["maximum"])


def test_the_mat_and_its_lights_take_the_contracts_ranges():
    sliders = _sliders("frame-mat.tsx")
    tenths = tuple(value / 10 for value in _bounds(Light, "strength_tenths"))
    assert sliders == {
        "Comes from": (*_bounds(Light, "from_degrees"), 1.0),
        "Steepness": (*_bounds(Light, "steep_degrees"), 1.0),
        "Softness": (*_bounds(Light, "softness_degrees"), 1.0),
        "Strength": (*tenths, 0.1),
        "Colour": (*_bounds(Light, "colour_kelvin"), float(Light.model_json_schema()["properties"]["colour_kelvin"]
                                                          ["multipleOf"])),
        "Depth": (0.0, MAT_DEPTH_MM_MAX, float(Mat.model_json_schema()["properties"]["depth_mm"]["multipleOf"])),
    }, sliders
    source = (DOMAIN / "frame-mat.tsx").read_text()
    assert re.search(rf"^export const LIGHTS_MAX = {MAT_LIGHTS_MAX};$", source, re.MULTILINE), "LIGHTS_MAX drifted"


def test_photo_walls_sliders_take_the_software_pictures_ranges():
    sliders = _sliders("frame-picture.tsx")
    assert sliders["Colour temperature"] == (KELVIN_MIN, KELVIN_MAX, KELVIN_STEP)
    assert sliders["Gamma"] == (GAMMA_TENTHS_MIN / 10, GAMMA_TENTHS_MAX / 10, 0.1)
    assert sliders["Black floor"] == (0.0, BLACK_FLOOR_TENTHS_MAX / 10, 0.1)
    assert sliders["LUT amount"] == (0.0, LUT_AMOUNT_PERCENT_MAX, 1.0)
    percents = {label: int(high) for label, high in PERCENT.findall((DOMAIN / "frame-picture.tsx").read_text())}
    assert percents == {"Brightness": SOFTWARE_PERCENT_MAX, "Contrast": SOFTWARE_PERCENT_MAX,
                        "Saturation": SATURATION_PERCENT_MAX, "Soft highlights": ROLLOFF_PERCENT_MAX}, percents
