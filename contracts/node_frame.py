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

One home each here: the operations' shape, ranges and codec; the picture change's wire text.
How settings become operations is Display Host's (appliance/display_host/picture.py); the final
pass that applies them is the Player's (player/geometry.py, player/native.py).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, fields
from hashlib import sha256
from typing import Final

from contracts.node_protocol import counter, digest, token
from contracts.strict_json import loads_object

PICTURE_OPERATIONS_BYTES: Final = 512     # one `picture` event's operations, encoded
LIGHT_FACTOR_MAX: Final = 2.0             # light_factor in 0..2 (Photo Wall's brightness 0..150 % is 0..1.5)
CONTRAST_SLOPE_MAX: Final = 2.0           # contrast_slope in 0..2
GAMMA_EXPONENT_MIN: Final = 0.5           # gamma_exponent in 0.5..2
GAMMA_EXPONENT_MAX: Final = 2.0
NO_PICTURE_CHANGE: Final = "0"            # tag_picture's text before any picture event reached the Player


def srgb_to_linear(value: float) -> float:
    """An sRGB-encoded channel (0..1, clamped) in linear light (IEC 61966-2-1): the one home of the
    curve the Player's final pass decodes with and the Pi's white balance is worked in. Pure."""
    value = min(1.0, max(0.0, value))
    return value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4


@dataclass(frozen=True)
class PictureOperations:
    """What the Player's final pass does to one Output's picture, in this order (1c design, Final
    pass order): multiply linear light by `rgb_multipliers` (white balance; the largest is 1),
    multiply by `light_factor`, clamp, encode for the screen, apply `contrast_slope` about
    mid-grey (0.5), raise to `gamma_exponent`, clamp, then zero everything outside the Output's
    Frame openings. Every value at its neutral (1) changes nothing. `picture_change` is the
    Output document's picture change these came from, echoed by the Player's tag."""
    output_id: str
    picture_change: int
    rgb_multipliers: tuple[float, float, float]
    light_factor: float
    contrast_slope: float
    gamma_exponent: float

    def __post_init__(self) -> None:
        """ValueError("picture_operations") unless output_id is a node token, picture_change >= 1,
        every number is a finite float (an int is accepted and kept as given, never a bool), each
        multiplier is in (0, 1] with the largest exactly 1.0, 0 <= light_factor <=
        LIGHT_FACTOR_MAX, 0 <= contrast_slope <= CONTRAST_SLOPE_MAX, and GAMMA_EXPONENT_MIN <=
        gamma_exponent <= GAMMA_EXPONENT_MAX."""
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
                 and _number(self.gamma_exponent, GAMMA_EXPONENT_MIN, GAMMA_EXPONENT_MAX))
        if not valid:
            raise ValueError("picture_operations")


def neutral_operations(output_id: str, picture_change: int) -> PictureOperations:
    """Operations that change nothing: multipliers (1, 1, 1) and every factor 1."""
    return PictureOperations(output_id=output_id, picture_change=picture_change, rgb_multipliers=(1.0, 1.0, 1.0),
                             light_factor=1.0, contrast_slope=1.0, gamma_exponent=1.0)


def encode_picture_operations(operations: PictureOperations) -> str:
    """Canonical JSON (sorted keys, no spaces, tuples as lists, floats as Python's repr) of at most
    PICTURE_OPERATIONS_BYTES; ValueError("picture_operations_too_large") past it."""
    value = {field.name: getattr(operations, field.name) for field in fields(PictureOperations)}
    value["rgb_multipliers"] = list(operations.rgb_multipliers)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(raw.encode()) > PICTURE_OPERATIONS_BYTES:
        raise ValueError("picture_operations_too_large")
    return raw


def decode_picture_operations(raw: str) -> PictureOperations:
    """The operations `raw` encodes (contracts.strict_json rules; exactly the six fields);
    ValueError("picture_operations") for anything else."""
    value = loads_object(raw.encode(), max_bytes=PICTURE_OPERATIONS_BYTES) if isinstance(raw, str) else None
    if value is None or set(value) != {field.name for field in fields(PictureOperations)}:
        raise ValueError("picture_operations")
    multipliers = value["rgb_multipliers"]
    if not isinstance(multipliers, list):
        raise ValueError("picture_operations")
    return PictureOperations(**{**value, "rgb_multipliers": tuple(multipliers)})


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
