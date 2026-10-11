"""Picture operations and the picture change text of frame protocol v4 (roadmap 1c, slice T0;
contracts/node_frame.py)."""
from __future__ import annotations

import json

import pytest

from contracts.node_frame import (
    NO_PICTURE_CHANGE,
    PICTURE_OPERATIONS_BYTES,
    PictureOperations,
    decode_picture_operations,
    encode_picture_operations,
    neutral_operations,
    parse_picture_change,
    picture_change_text,
)

WARM = PictureOperations("HDMI-A-1", 6, (1.0, 0.7114, 0.4250), 0.8, 0.5, 24 / 22)


def _operations(**changes) -> PictureOperations:
    fields = {"output_id": "HDMI-A-1", "picture_change": 6, "rgb_multipliers": (1.0, 1.0, 1.0),
              "light_factor": 1.0, "contrast_slope": 1.0, "gamma_exponent": 1.0}
    return PictureOperations(**{**fields, **changes})


@pytest.mark.parametrize("operations", [WARM, neutral_operations("Virtual-1", 1),
                                        _operations(light_factor=0, contrast_slope=2, gamma_exponent=0.5)])
def test_operations_round_trip(operations):
    raw = encode_picture_operations(operations)
    assert decode_picture_operations(raw) == operations
    assert encode_picture_operations(decode_picture_operations(raw)) == raw


def test_the_encoding_is_canonical_json():
    raw = encode_picture_operations(WARM)
    assert raw == json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))
    assert '"rgb_multipliers":[1.0,0.7114,0.425]' in raw
    shuffled = json.dumps(dict(reversed(json.loads(raw).items())))
    assert encode_picture_operations(decode_picture_operations(shuffled)) == raw


def test_neutral_operations_change_nothing():
    assert neutral_operations("HDMI-A-2", 9) == PictureOperations("HDMI-A-2", 9, (1.0, 1.0, 1.0), 1.0, 1.0, 1.0)


@pytest.mark.parametrize("changes", [
    {"rgb_multipliers": (0.99, 0.5, 0.5)},              # the largest multiplier below 1
    {"rgb_multipliers": (1.0, 0.0, 0.5)},               # a multiplier 0
    {"rgb_multipliers": (1.0, 1.01, 0.5)},              # above 1
    {"rgb_multipliers": (1.0, 1.0)},
    {"rgb_multipliers": [1.0, 1.0, 1.0]},               # not a tuple
    {"rgb_multipliers": (True, 1.0, 1.0)},
    {"light_factor": 2.01},
    {"light_factor": -0.1},
    {"contrast_slope": 2.5},
    {"gamma_exponent": 0.4},
    {"gamma_exponent": 2.1},
    {"gamma_exponent": float("nan")},
    {"light_factor": float("inf")},
    {"light_factor": True},
    {"picture_change": 0},
    {"picture_change": True},
    {"output_id": ""},
], ids=["largest-below-1", "multiplier-0", "multiplier-above-1", "two-multipliers", "list-multipliers",
        "bool-multiplier", "light-2.01", "light-negative", "contrast-2.5", "gamma-0.4", "gamma-2.1", "nan",
        "infinite", "bool-light", "change-0", "bool-change", "empty-output"])
def test_operations_refuse(changes):
    with pytest.raises(ValueError, match="^picture_operations$"):
        _operations(**changes)


@pytest.mark.parametrize("raw", [
    encode_picture_operations(WARM).replace('"light_factor":0.8', '"light_factor":NaN'),
    encode_picture_operations(WARM)[:-1] + ',"unknown":1}',
    encode_picture_operations(WARM)[:-1] + ',"light_factor":0.8}',            # duplicate key
    json.dumps({key: value for key, value in json.loads(encode_picture_operations(WARM)).items()
                if key != "gamma_exponent"}),
    encode_picture_operations(WARM).replace('"rgb_multipliers":[1.0,0.7114,0.425]', '"rgb_multipliers":"white"'),
    encode_picture_operations(WARM) + " " * (PICTURE_OPERATIONS_BYTES + 1 - len(encode_picture_operations(WARM))),
    "[]",
    "not json",
], ids=["nan", "unknown-field", "duplicate-key", "missing-field", "multipliers-not-a-list", "past-512-bytes",
        "not-object", "not-json"])
def test_decoding_refuses(raw):
    with pytest.raises(ValueError, match="^picture_operations$"):
        decode_picture_operations(raw)


def test_the_largest_operations_fit_the_event():
    long_id = "a" * 128   # a node token's longest
    raw = encode_picture_operations(_operations(output_id=long_id, rgb_multipliers=(1.0, 1 / 3, 1 / 7)))
    assert len(raw) <= PICTURE_OPERATIONS_BYTES   # the largest real operations fit with room to spare


def test_the_picture_change_text_both_ways():
    assert picture_change_text(None) == NO_PICTURE_CHANGE == "0"
    assert picture_change_text(17) == "17"
    assert parse_picture_change("0") is None
    assert parse_picture_change("17") == 17
    assert parse_picture_change(picture_change_text(2**63 - 1)) == 2**63 - 1
    for text in ("017", "-1", "+1", " 1", "1.0", "", "x", str(2**63)):
        with pytest.raises(ValueError, match="^picture_change$"):
            parse_picture_change(text)
    for change in (0, -1, True, 2**63):
        with pytest.raises(ValueError, match="^picture_change$"):
            picture_change_text(change)
