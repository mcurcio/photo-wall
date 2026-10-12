"""The picture document and report on their own keys (decision 0022; the picture split, slice S1;
contracts/node_picture.py and the shared codec contracts/wire_codec.py).

Every type round-trips through its codec; the bytes are canonical; each validator refuses its
named bad case; the worst valid report still fits the bus's cap, so no valid report is ever
refused for size.
"""
from __future__ import annotations

import json

import pytest

from contracts.node_output import DisplayIdentity
from contracts.node_picture import (
    CHOICE_NAME_MAX,
    LEVEL_MAX,
    MAX_CHOICES,
    NEUTRAL_SOFTWARE,
    PICTURE_DOCUMENT_BYTES,
    PICTURE_LIST,
    PICTURE_REPORT_BYTES,
    Choice,
    HardwareRange,
    HardwareResult,
    HardwareSetting,
    PictureDocument,
    PictureReport,
    RangeKind,
    SoftwarePicture,
    WriteResult,
    decode_picture_document,
    decode_picture_report,
    encode_picture_document,
    encode_picture_report,
    picture_document_key,
    picture_report_key,
)

IDENTITY = DisplayIdentity("XYM", 5475, "MNN", None)
SOFTWARE = SoftwarePicture(114, 100, 6500, 22)
DOCUMENT = PictureDocument("HDMI-A-1", 8, {"10": 46}, SOFTWARE)
LEVEL = HardwareRange(RangeKind.LEVEL, 100, ())
PRESETS = HardwareRange(RangeKind.CHOICE, None, (Choice(5, "6500 K"), Choice(11, "User 1")))
READ_ONLY = HardwareRange(RangeKind.READ_ONLY_CHOICE, None, ())
REPORT = PictureReport("HDMI-A-1", IDENTITY,
                       (HardwareSetting("10", LEVEL, 60), HardwareSetting("14", PRESETS, 5),
                        HardwareSetting("DC", READ_ONLY, None)),
                       8, {"10": HardwareResult(WriteResult.TAKEN, 60, 46)}, 8)

# The frozen page's wire shapes, byte for byte.
DOCUMENT_BYTES = (b'{"hardware":{"10":46},"output_id":"HDMI-A-1","picture_change":8,"software":'
                  b'{"brightness_percent":114,"colour_temperature_kelvin":6500,"contrast_percent":100,'
                  b'"gamma_tenths":22}}')


def test_the_document_is_the_frozen_wire_shape_and_round_trips():
    assert encode_picture_document(DOCUMENT) == DOCUMENT_BYTES
    assert decode_picture_document(DOCUMENT_BYTES) == DOCUMENT
    reordered = json.dumps(json.loads(DOCUMENT_BYTES), sort_keys=False, indent=1).encode()
    assert encode_picture_document(decode_picture_document(reordered)) == DOCUMENT_BYTES


def test_the_report_is_the_frozen_wire_shape_and_round_trips():
    raw = encode_picture_report(REPORT)
    value = json.loads(raw)
    assert set(value) == {"for_change", "identity", "offered", "output_id", "presented_change", "results"}
    assert value["offered"][0] == {"code": "10", "current": 60, "range": {"choices": [], "kind": "level", "maximum": 100}}
    assert value["offered"][1]["range"]["choices"] == [{"name": "6500 K", "value": 5}, {"name": "User 1", "value": 11}]
    assert value["results"] == {"10": {"after": 46, "before": 60, "result": "taken"}}
    assert "name" not in value["offered"][0]          # the name is PICTURE_LIST's, never the wire's
    assert decode_picture_report(raw) == REPORT
    unknown = PictureReport("HDMI-A-2", None, None, None, {}, None)
    assert decode_picture_report(encode_picture_report(unknown)) == unknown
    asked_none = PictureReport("HDMI-A-2", IDENTITY, (), 3, {}, None)
    assert decode_picture_report(encode_picture_report(asked_none)) == asked_none


def test_the_keys_and_the_neutral_software():
    assert picture_document_key("HDMI-A-1") == picture_report_key("HDMI-A-1") == "picture-HDMI-A-1"
    for key in (picture_document_key, picture_report_key):
        with pytest.raises(ValueError, match="^output_id$"):
            key("DSI-1")
    assert NEUTRAL_SOFTWARE == SoftwarePicture(100, 100, 6500, 22)


def test_the_worst_valid_report_fits_the_bus():
    """30 offered settings, each a CHOICE of 16 choices with 24-character names, a result per code
    at the largest values: under PICTURE_REPORT_BYTES."""
    choices = tuple(Choice(value, "~" * CHOICE_NAME_MAX) for value in range(240, 240 + MAX_CHOICES))
    offered = tuple(HardwareSetting(code, HardwareRange(RangeKind.CHOICE, None, choices), 255) for code in PICTURE_LIST)
    worst = PictureReport("Virtual-2", DisplayIdentity("XYM", 65535, "M" * 13, "S" * 32), offered, 2**62,
                          {code: HardwareResult(WriteResult.NOT_TAKEN, LEVEL_MAX, LEVEL_MAX) for code in PICTURE_LIST},
                          2**62)
    assert len(PICTURE_LIST) == 30
    raw = encode_picture_report(worst)
    assert len(raw) <= PICTURE_REPORT_BYTES and decode_picture_report(raw) == worst
    every_code = PictureDocument("Virtual-2", 2**62, dict.fromkeys(PICTURE_LIST, LEVEL_MAX),
                                 SoftwarePicture(150, 150, 9300, 26))
    assert len(encode_picture_document(every_code)) <= PICTURE_DOCUMENT_BYTES


@pytest.mark.parametrize(("build", "code"), [
    (lambda: Choice(256, "x"), "choice"),
    (lambda: Choice(1, ""), "choice"),
    (lambda: Choice(1, "x" * (CHOICE_NAME_MAX + 1)), "choice"),
    (lambda: Choice(1, "é"), "choice"),
    (lambda: Choice(1, 'a "b"'), "choice"),        # a JSON escape would let a valid report outgrow the cap
    (lambda: Choice(True, "x"), "choice"),
    (lambda: HardwareRange(RangeKind.LEVEL, 0, ()), "hardware_range"),
    (lambda: HardwareRange(RangeKind.LEVEL, LEVEL_MAX + 1, ()), "hardware_range"),
    (lambda: HardwareRange(RangeKind.LEVEL, 100, (Choice(1, "x"),)), "hardware_range"),
    (lambda: HardwareRange(RangeKind.CHOICE, None, ()), "hardware_range"),
    (lambda: HardwareRange(RangeKind.CHOICE, 100, (Choice(1, "x"),)), "hardware_range"),
    (lambda: HardwareRange(RangeKind.CHOICE, None, (Choice(2, "x"), Choice(1, "y"))), "hardware_range"),
    (lambda: HardwareRange(RangeKind.CHOICE, None, tuple(Choice(v, "x") for v in range(MAX_CHOICES + 1))),
     "hardware_range"),
    (lambda: HardwareRange(RangeKind.READ_ONLY_CHOICE, None, (Choice(1, "x"),)), "hardware_range"),
    (lambda: HardwareSetting("D6", LEVEL, 1), "hardware_setting"),      # power is not on the picture list
    (lambda: HardwareSetting("e0", LEVEL, 1), "hardware_setting"),
    (lambda: HardwareSetting("10", LEVEL, 101), "hardware_setting"),
    (lambda: HardwareSetting("14", PRESETS, 256), "hardware_setting"),
    (lambda: SoftwarePicture(151, 100, 6500, 22), "software_picture"),
    (lambda: SoftwarePicture(100, -1, 6500, 22), "software_picture"),
    (lambda: SoftwarePicture(100, 100, 6550, 22), "software_picture"),
    (lambda: SoftwarePicture(100, 100, 6500, 27), "software_picture"),
    (lambda: SoftwarePicture(True, 100, 6500, 22), "software_picture"),
    (lambda: PictureDocument("DSI-1", 1, {}, SOFTWARE), "picture_document"),
    (lambda: PictureDocument("HDMI-A-1", 0, {}, SOFTWARE), "picture_document"),
    (lambda: PictureDocument("HDMI-A-1", 1, {"60": 1}, SOFTWARE), "picture_document"),   # input: not picture
    (lambda: PictureDocument("HDMI-A-1", 1, {"10": LEVEL_MAX + 1}, SOFTWARE), "picture_document"),
    (lambda: PictureDocument("HDMI-A-1", 1, {}, None), "picture_document"),
    (lambda: HardwareResult(WriteResult.TAKEN, 1, None), "hardware_result"),
    (lambda: HardwareResult(WriteResult.NO_ANSWER, 1, 2), "hardware_result"),
    (lambda: HardwareResult(WriteResult.NOT_TAKEN, -1, 2), "hardware_result"),
    (lambda: HardwareResult("taken", 1, 1), "hardware_result"),
    (lambda: PictureReport("DSI-1", None, None, None, {}, None), "picture_report"),
    (lambda: PictureReport("HDMI-A-1", None, (HardwareSetting("12", LEVEL, 1), HardwareSetting("10", LEVEL, 1)),
                           None, {}, None), "picture_report"),                      # out of list order
    (lambda: PictureReport("HDMI-A-1", None, (HardwareSetting("10", LEVEL, 1),) * 2, None, {}, None),
     "picture_report"),
    (lambda: PictureReport("HDMI-A-1", None, None, None, {"10": HardwareResult(WriteResult.TAKEN, 1, 1)}, None),
     "picture_report"),                                                             # results without a change
    (lambda: PictureReport("HDMI-A-1", None, None, 1, {"D6": HardwareResult(WriteResult.TAKEN, 1, 1)}, None),
     "picture_report"),
    (lambda: PictureReport("HDMI-A-1", None, None, 0, {}, None), "picture_report"),
    (lambda: PictureReport("HDMI-A-1", None, None, None, {}, 0), "picture_report"),
])
def test_each_validator_refuses_its_bad_case(build, code):
    with pytest.raises(ValueError, match=f"^{code}$"):
        build()


@pytest.mark.parametrize("raw", [
    b"",
    b"[]",
    DOCUMENT_BYTES.replace(b'"picture_change":8', b'"picture_change":8,"extra":1'),
    DOCUMENT_BYTES.replace(b'{"10":46}', b'[46]'),
    DOCUMENT_BYTES.replace(b'{"10":46}', b'{"10":46.0}'),
    DOCUMENT_BYTES.replace(b'"gamma_tenths":22', b'"gamma_tenths":22,"gamma_tenths":22'),
    b" " * (PICTURE_DOCUMENT_BYTES + 1),
])
def test_decoding_a_document_refuses(raw):
    with pytest.raises(ValueError, match="^picture_document$"):
        decode_picture_document(raw)


def test_decoding_a_report_refuses():
    good = json.loads(encode_picture_report(REPORT))
    for broken in ({**good, "offered": {}}, {**good, "results": []},
                   {**good, "offered": [{**good["offered"][0], "name": "Brightness"}]},
                   {**good, "offered": [{**good["offered"][0], "range": {"kind": "slider", "maximum": 1, "choices": []}}]},
                   {**good, "results": {"10": {"result": "maybe", "before": 1, "after": 1}}}):
        with pytest.raises(ValueError, match="^picture_report$"):
            decode_picture_report(json.dumps(broken).encode())
    with pytest.raises(ValueError, match="^picture_report$"):
        decode_picture_report(b" " * (PICTURE_REPORT_BYTES + 1))
