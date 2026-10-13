"""Correlation of an actual normal app draw with its compositor commit.

This tag is a witness, never execution or display admission authority. The
renderer supplies its actual ordered visible layers; an operational Trial has a
separate tag and cannot impersonate normal representative media.

Frame protocol v4 (roadmap 1c; run ledger .claude/runs/display-1c.md;
appliance/display_host/native/photo-wall-frame-v1.xml) adds the picture:
- the `picture` event carries `PictureOperations` (Display Host's controller -> shell -> the
  Player, one per Output) as canonical JSON of at most PICTURE_OPERATIONS_BYTES. Operations, not
  settings: the Player applies them with no table and knows nothing of displays, DDC/CI or
  Central (owner steer 2026-10-10: what the Player receives says what it means);
- the `tag_picture` request names, beside the frame tag, the picture change the tagged buffer
  was drawn with; the shell echoes it in its `presented` event, so the controller reports the
  newest picture change the Player presented. Its wire text is `picture_change_text`'s.

Picture operations v5 (the print look, 2026-10-12) add the print look's steps: `saturation`,
`highlight_knee` (the highlight rolloff, which replaces the clamp after white balance x
brightness), `black_floor` (the last tone step) and `lut` (`LutMix`: a LUT by name and how much of
it is mixed in). Each at its neutral value (1, 1, 0, None) is skipped by the Player, never
computed, so neutral operations draw exactly the v4 pixels. The Wayland messages are unchanged
(the interface stays version 4); the `picture` event's operations text grows to
PICTURE_OPERATIONS_BYTES, and a Player and a display host from different releases refuse each
other's text (the strict codec below), as a Pi's release ships whole.

Picture operations v6 (display tuning, decision 0024) add `test_pattern` (contracts/tuning.py
`TestPattern`): None draws the Frame's photos as before; WHITE and GREY_RAMP replace the photos
inside every Frame's opening on the Output, Position's grid included, and pass through every step
above and the mat; BLACK_LEVEL draws raw signal values through no step and no mat. Outside every
opening stays black for all three. The Wayland messages are unchanged.

One home each here: the operations' shape, ranges and codec; the picture change's wire text; the
knee's and the floor's bounds. How settings become operations is Display Host's
(appliance/display_host/picture.py `operations_for`); what each operation does, in which order,
is the Player's (player/geometry.py, the scalar reference, mirrored by player/native.py).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, fields
from hashlib import sha256
from typing import Final

from contracts.lut import LutRef
from contracts.node_protocol import counter, digest, token
from contracts.strict_json import loads_object
from contracts.tuning import TestPattern

PICTURE_OPERATIONS_BYTES: Final = 1024    # one `picture` event's operations, encoded (v5 and v6; 512 in v4)
LIGHT_FACTOR_MAX: Final = 2.0             # light_factor in 0..2 (Photo Wall's brightness 0..150 % is 0..1.5)
CONTRAST_SLOPE_MAX: Final = 2.0           # contrast_slope in 0..2
GAMMA_EXPONENT_MIN: Final = 0.5           # gamma_exponent in 0.5..2
GAMMA_EXPONENT_MAX: Final = 2.0
NO_PICTURE_CHANGE: Final = "0"            # tag_picture's text before any picture event reached the Player
SATURATION_MAX: Final = 1.5               # saturation in 0..1.5 (Photo Wall's 0..150 %); 1 is skipped
HIGHLIGHT_KNEE_MIN: Final = 0.18          # highlight_knee in 0.18..1, linear light (0.18: mid-grey); 1 is skipped.
                                          # The rolloff slider's 100 % (a placeholder, tuned on the bench)
BLACK_FLOOR_MAX: Final = 0.1              # black_floor in 0..0.1, a share of white light (Photo Wall's
                                          # 0..10 % of white); 0 is skipped. The Player lifts in the panel's
                                          # light (sRGB, the panel its encode is for), never in output values


def srgb_to_linear(value: float) -> float:
    """An sRGB-encoded channel (0..1, clamped) in linear light (IEC 61966-2-1): the one home of the
    curve the Player's final pass decodes with and the Pi's white balance is worked in. Pure."""
    value = min(1.0, max(0.0, value))
    return value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4


@dataclass(frozen=True)
class LutMix:
    """A LUT the Player applies (step 1 of the print look's order), by name (`ref`, fetched from
    Central), mixed in by `amount` (0 < amount <= 1: a LUT at amount 0 is no LutMix at all)."""
    ref: LutRef
    amount: float

    def __post_init__(self) -> None:
        """ValueError("picture_operations") unless ref is a LutRef and amount a finite float (or an
        int, never a bool) with 0 < amount <= 1."""
        if not (isinstance(self.ref, LutRef) and _number(self.amount, 0, 1) and self.amount > 0):
            raise ValueError("picture_operations")


@dataclass(frozen=True)
class PictureOperations:
    """What the Player's final pass does to one Output's picture (v5). The order, each step's
    formula and its skip rule are the Player's (player/geometry.py `final_pixel`); in brief: mix in
    the LUT (`lut`), `saturation` about luminance, multiply linear light by `rgb_multipliers` (white
    balance; the largest is 1) and `light_factor`, roll the highlights off above `highlight_knee`
    (or clamp, as v4, when skipped), encode for the screen, apply `contrast_slope` about mid-grey
    (0.5), raise to `gamma_exponent`, clamp, lift black to `black_floor` of white light, then the Frame's mat shadow
    (from its binding, never from here) and black outside the Output's Frame openings. Every value
    at its neutral (multipliers and factors 1, knee 1, floor 0, no LUT) changes nothing.
    `picture_change` is the picture document's change these came from, echoed by the Player's tag.
    `test_pattern` (v6): drawn instead of the Frame's photos (the module's v6 rule; the pattern's
    layout is player/geometry.py `test_pattern_signal`); None: the photos.

    The print look's and display tuning's fields default to their neutral values in Python; the
    wire never defaults them (`decode_picture_operations` needs every field)."""
    output_id: str
    picture_change: int
    rgb_multipliers: tuple[float, float, float]
    light_factor: float
    contrast_slope: float
    gamma_exponent: float
    saturation: float = 1.0
    highlight_knee: float = 1.0
    black_floor: float = 0.0
    lut: LutMix | None = None
    test_pattern: TestPattern | None = None

    def __post_init__(self) -> None:
        """ValueError("picture_operations") unless output_id is a node token, picture_change >= 1,
        every number is a finite float (an int is accepted and kept as given, never a bool), each
        multiplier is in (0, 1] with the largest exactly 1.0, 0 <= light_factor <=
        LIGHT_FACTOR_MAX, 0 <= contrast_slope <= CONTRAST_SLOPE_MAX, GAMMA_EXPONENT_MIN <=
        gamma_exponent <= GAMMA_EXPONENT_MAX, 0 <= saturation <= SATURATION_MAX, HIGHLIGHT_KNEE_MIN
        <= highlight_knee <= 1, 0 <= black_floor <= BLACK_FLOOR_MAX, lut is None or a LutMix, and
        test_pattern is None or a TestPattern."""
        multipliers = self.rgb_multipliers
        try:
            token(self.output_id)
            counter(self.picture_change, 1)
        except ValueError as error:
            raise ValueError("picture_operations") from error
        valid = (isinstance(multipliers, tuple) and len(multipliers) == 3
                 and all(_number(value, 0, 1) and value > 0 for value in multipliers)
                 and max(multipliers) == 1.0
                 and _number(self.light_factor, 0, LIGHT_FACTOR_MAX)
                 and _number(self.contrast_slope, 0, CONTRAST_SLOPE_MAX)
                 and _number(self.gamma_exponent, GAMMA_EXPONENT_MIN, GAMMA_EXPONENT_MAX)
                 and _number(self.saturation, 0, SATURATION_MAX)
                 and _number(self.highlight_knee, HIGHLIGHT_KNEE_MIN, 1)
                 and _number(self.black_floor, 0, BLACK_FLOOR_MAX)
                 and (self.lut is None or isinstance(self.lut, LutMix))
                 and (self.test_pattern is None or isinstance(self.test_pattern, TestPattern)))
        if not valid:
            raise ValueError("picture_operations")


def neutral_operations(output_id: str, picture_change: int) -> PictureOperations:
    """Operations that change nothing: multipliers (1, 1, 1), every factor 1, knee 1, floor 0, no
    LUT, no test pattern."""
    return PictureOperations(output_id=output_id, picture_change=picture_change, rgb_multipliers=(1.0, 1.0, 1.0),
                             light_factor=1.0, contrast_slope=1.0, gamma_exponent=1.0, saturation=1.0,
                             highlight_knee=1.0, black_floor=0.0, lut=None, test_pattern=None)


def encode_picture_operations(operations: PictureOperations) -> str:
    """Canonical JSON (sorted keys, no spaces, tuples as lists, a LutMix and its LutRef as objects of
    their fields, a test pattern as its value, floats as Python's repr) of at most PICTURE_OPERATIONS_BYTES;
    ValueError("picture_operations_too_large") past it (never for valid operations: every field at
    its longest fits)."""
    value = {field.name: getattr(operations, field.name) for field in fields(PictureOperations)}
    value["rgb_multipliers"] = list(operations.rgb_multipliers)
    mix = operations.lut
    value["lut"] = None if mix is None else {"amount": mix.amount, "ref": {
        "profile_sha256": mix.ref.profile_sha256, "recipe_id": mix.ref.recipe_id}}
    value["test_pattern"] = None if operations.test_pattern is None else operations.test_pattern.value
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(raw.encode()) > PICTURE_OPERATIONS_BYTES:
        raise ValueError("picture_operations_too_large")
    return raw


def decode_picture_operations(raw: str) -> PictureOperations:
    """The operations `raw` encodes (contracts.strict_json rules; exactly the eleven fields, a lut
    exactly {amount, ref} and its ref exactly {profile_sha256, recipe_id}, a test pattern null or
    one of TestPattern's values);
    ValueError("picture_operations") for anything else."""
    value = loads_object(raw.encode(), max_bytes=PICTURE_OPERATIONS_BYTES) if isinstance(raw, str) else None
    if value is None or set(value) != {field.name for field in fields(PictureOperations)}:
        raise ValueError("picture_operations")
    multipliers, mix = value["rgb_multipliers"], value["lut"]
    if not isinstance(multipliers, list):
        raise ValueError("picture_operations")
    if mix is not None:
        if not (isinstance(mix, dict) and set(mix) == {"amount", "ref"} and isinstance(mix["ref"], dict)
                and set(mix["ref"]) == {"profile_sha256", "recipe_id"}):
            raise ValueError("picture_operations")
        try:
            mix = LutMix(LutRef(**mix["ref"]), mix["amount"])
        except ValueError as error:
            raise ValueError("picture_operations") from error
    pattern = value["test_pattern"]
    if pattern is not None:
        try:
            pattern = TestPattern(pattern)
        except (ValueError, TypeError) as error:
            raise ValueError("picture_operations") from error
    return PictureOperations(**{**value, "rgb_multipliers": tuple(multipliers), "lut": mix, "test_pattern": pattern})


def picture_change_text(change: int | None) -> str:
    """tag_picture's picture change argument: the decimal change, or NO_PICTURE_CHANGE for None.
    ValueError("picture_change") for a change below 1."""
    if change is None:
        return NO_PICTURE_CHANGE
    _picture_change(change)
    return str(change)


def parse_picture_change(text: str) -> int | None:
    """The inverse of `picture_change_text`: None for NO_PICTURE_CHANGE; ValueError("picture_change")
    for anything but a canonical decimal of 1..2**63-1."""
    if text == NO_PICTURE_CHANGE:
        return None
    if not isinstance(text, str) or _CANONICAL_CHANGE.fullmatch(text) is None:
        raise ValueError("picture_change")
    return _picture_change(int(text))


_CANONICAL_CHANGE: Final = re.compile(r"[1-9][0-9]{0,18}")


def _picture_change(change: int) -> int:
    """`change` when it is a node counter of at least 1; ValueError("picture_change") otherwise."""
    try:
        counter(change, 1)
    except ValueError as error:
        raise ValueError("picture_change") from error
    return change


def _number(value: object, minimum: float, maximum: float) -> bool:
    """A finite float or int (never a bool) in minimum..maximum."""
    return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum


def frame_witness_tag(*, output_id: str, frame_id: str, binding_generation: int, config_revision: int,
                      calibration: dict, layers: tuple[dict, ...]) -> str:
    token(output_id)
    token(frame_id)
    counter(binding_generation, 1)
    counter(config_revision, 1)
    if type(calibration) is not dict or type(layers) is not tuple or len(layers) > 64:
        raise ValueError("node_frame_witness_invalid")
    seen = set()
    for layer in layers:
        if type(layer) is not dict or set(layer) != {"assignment_id", "variant_sha256"}:
            raise ValueError("node_frame_layer_invalid")
        token(layer["assignment_id"])
        if layer["assignment_id"] in seen:
            raise ValueError("node_frame_layer_duplicate")
        seen.add(layer["assignment_id"])
        if layer["variant_sha256"] is not None:
            digest(layer["variant_sha256"])
    value = {"output_id": output_id, "frame_id": frame_id, "binding_generation": binding_generation,
             "config_revision": config_revision, "calibration": calibration, "layers": layers}
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > 16384:
        raise ValueError("node_frame_witness_too_large")
    return sha256(b"photo-wall-app-frame-v2\0"+raw).hexdigest()
