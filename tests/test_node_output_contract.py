"""The Output document and report on the wire (roadmap 1b, slice T1; contracts/node_output.py).

Every type round-trips through its codec; a document's bytes are canonical (one encoding however its
input was ordered, so a digest of them is stable); and each refusal the contract names is refused
with the type's own code.
"""
from __future__ import annotations

import itertools
import json

import pytest

from contracts.node_output import (
    AS_THE_DISPLAY_HAS_IT,
    BEST_DETECTED,
    MAX_POWER_REQUESTS,
    OUTPUT_DOCUMENT_BYTES,
    ActsOn,
    DisplayIdentity,
    DisplayMode,
    InForce,
    OutputDocument,
    OutputPicture,
    OutputReport,
    PictureReport,
    PictureSetting,
    Power,
    PowerAttempt,
    PowerMethod,
    PowerRequest,
    PowerResult,
    RequestReason,
    SettingReport,
    WriteResult,
    decode_output_document,
    decode_output_report,
    decode_power_attempt,
    encode_output_document,
    encode_output_report,
    encode_power_attempt,
    output_document_key,
    output_report_key,
)

STANDING = PowerRequest("standing", Power.ON, RequestReason.STANDING)
TEST_OFF = PowerRequest("req-7", Power.OFF, RequestReason.CONSOLE_TEST, for_seconds=300)
PICTURE = OutputPicture(PictureSetting(ActsOn.DISPLAY, 60), PictureSetting(ActsOn.PICTURE_ADJUSTMENT, AS_THE_DISPLAY_HAS_IT),
                        3000, 24)
DOCUMENT = OutputDocument("HDMI-A-1", 3, (TEST_OFF, STANDING), PowerMethod.DDC_CI, True, True, 2, PICTURE)
IDENTITY = DisplayIdentity("XYM", 5475, "MNN", None)
REPORT = OutputReport(
    "HDMI-A-1", True, IDENTITY,
    (DisplayMode(1920, 1080, 60000, preferred=True), DisplayMode(1280, 720, 59940)),
    (PowerMethod.DDC_CI, PowerMethod.SIGNAL_OFF), PowerMethod.DDC_CI, 3, PowerResult.CONFIRMED,
    InForce("req-7", 120))
ATTEMPT = PowerAttempt("HDMI-A-1", 3, "req-7", Power.OFF, PowerMethod.DDC_CI, PowerResult.CONFIRMED, 840)
CANONICAL = (b'{"change":3,"method":"ddc-ci","never_off_on_other_input":true,"output_id":"HDMI-A-1",'
             b'"picture":{"brightness":{"acts_on":"display","value":60},"colour_temperature_kelvin":3000,'
             b'"contrast":{"acts_on":"picture-adjustment","value":"as-the-display-has-it"},"gamma_tenths":24},'
             b'"picture_change":2,"power":[{"for_seconds":300,"power":"off","reason":"console-test","request_id":"req-7"},'
             b'{"for_seconds":null,"power":"on","reason":"standing","request_id":"standing"}],'
             b'"switch_input_on_power_on":true}')


@pytest.mark.parametrize("value, encode, decode", [
    (DOCUMENT, encode_output_document, decode_output_document),
    (OutputDocument("Virtual-2", 1, (STANDING,), BEST_DETECTED, False, False, 1, PICTURE),
     encode_output_document, decode_output_document),
    (REPORT, encode_output_report, decode_output_report),
    (OutputReport("HDMI-A-2", False, None, (), (), None, None, None, None),
     encode_output_report, decode_output_report),
    (OutputReport("HDMI-A-2", True, DisplayIdentity("ABC", 0, "", "SN 0042"), (), (PowerMethod.HDMI_CEC,),
                  PowerMethod.HDMI_CEC, 9, PowerResult.ANOTHER_INPUT, InForce("standing", None)),
     encode_output_report, decode_output_report),
    (ATTEMPT, encode_power_attempt, decode_power_attempt),
    (PowerAttempt("HDMI-A-2", 1, "standing", Power.ON, None, PowerResult.NOT_SUPPORTED, 0),
     encode_power_attempt, decode_power_attempt),
])
def test_every_type_round_trips(value, encode, decode):
    assert decode(encode(value)) == value
    assert encode(decode(encode(value))) == encode(value)


def test_a_documents_bytes_are_canonical_whatever_the_input_order():
    assert encode_output_document(DOCUMENT) == CANONICAL
    assert encode_output_document(DOCUMENT) == encode_output_document(DOCUMENT)
    parsed = json.loads(CANONICAL)
    for order in itertools.permutations(parsed):
        shuffled = {key: parsed[key] for key in order}
        shuffled["power"] = [dict(reversed(request.items())) for request in parsed["power"]]
        raw = json.dumps(shuffled).encode()
        assert raw != CANONICAL
        assert encode_output_document(decode_output_document(raw)) == CANONICAL


def test_the_bus_keys():
    assert output_document_key("HDMI-A-1") == "output-HDMI-A-1"
    assert output_report_key("HDMI-A-2") == "output-HDMI-A-2"
    for key in (output_document_key, output_report_key):
        with pytest.raises(ValueError, match="output_id"):
            key("HDMI-A-3")


def _document(**changes) -> dict:
    return {**json.loads(CANONICAL), **changes}


@pytest.mark.parametrize("build", [
    lambda: OutputDocument("HDMI-A-3", 1, (STANDING,), BEST_DETECTED, True, True, 1, PICTURE),
    lambda: OutputDocument("HDMI-A-1", 0, (STANDING,), BEST_DETECTED, True, True, 1, PICTURE),
    lambda: OutputDocument("HDMI-A-1", 1, (), BEST_DETECTED, True, True, 1, PICTURE),
    lambda: OutputDocument("HDMI-A-1", 1, (STANDING, TEST_OFF), BEST_DETECTED, True, True, 1, PICTURE),   # timed last
    lambda: OutputDocument("HDMI-A-1", 1, (TEST_OFF,), BEST_DETECTED, True, True, 1, PICTURE),
    lambda: OutputDocument("HDMI-A-1", 1, (TEST_OFF, TEST_OFF, STANDING), BEST_DETECTED, True, True, 1, PICTURE),
    lambda: OutputDocument("HDMI-A-1", 1, (STANDING,) * 2, BEST_DETECTED, True, True, 1, PICTURE),
    lambda: OutputDocument("HDMI-A-1", 1, tuple(PowerRequest(f"r{index}", Power.ON, RequestReason.STANDING)
                                               for index in range(MAX_POWER_REQUESTS + 1)), BEST_DETECTED, True, True,
                           1, PICTURE),
    lambda: OutputDocument("HDMI-A-1", 1, (STANDING,), "smart-plug", True, True, 1, PICTURE),
], ids=["unknown-output", "change-0", "empty-stack", "timed-last", "only-timed", "duplicate-ids-timed",
        "duplicate-ids", "too-deep", "unknown-method"])
def test_a_document_refuses(build):
    with pytest.raises(ValueError, match="^output_document$"):
        build()


@pytest.mark.parametrize("build", [
    lambda: PowerRequest("", Power.ON, RequestReason.STANDING),
    lambda: PowerRequest("standing", "on", RequestReason.STANDING),
    lambda: PowerRequest("req", Power.OFF, RequestReason.CONSOLE_TEST, for_seconds=0),
    lambda: PowerRequest("req", Power.OFF, RequestReason.CONSOLE_TEST, for_seconds=24 * 3600 + 1),
])
def test_a_power_request_refuses(build):
    with pytest.raises(ValueError, match="^power_request$"):
        build()


def _report(**changes) -> OutputReport:
    fields = {"output_id": "HDMI-A-1", "connected": True, "identity": IDENTITY, "modes": (),
              "answers": (PowerMethod.HDMI_CEC, PowerMethod.SIGNAL_OFF), "method": PowerMethod.SIGNAL_OFF,
              "for_change": 1, "result": None, "in_force": None}
    return OutputReport(**{**fields, **changes})


@pytest.mark.parametrize("changes", [
    {"output_id": "DP-1"},
    {"method": PowerMethod.DDC_CI},                                                   # not among answers
    {"answers": (PowerMethod.SIGNAL_OFF, PowerMethod.HDMI_CEC)},                      # out of precedence
    {"answers": (PowerMethod.SIGNAL_OFF, PowerMethod.SIGNAL_OFF)},
    {"for_change": 0},
    {"modes": (DisplayMode(640, 480, 60000),) * 65},
], ids=["unknown-output", "method-not-answered", "answers-out-of-order", "answers-repeated", "change-0",
        "too-many-modes"])
def test_a_report_refuses(changes):
    with pytest.raises(ValueError, match="^output_report$"):
        _report(**changes)


@pytest.mark.parametrize("build, code", [
    (lambda: DisplayIdentity("xym", 1, "", None), "display_identity"),
    (lambda: DisplayIdentity("XYM", 65536, "", None), "display_identity"),
    (lambda: DisplayIdentity("XYM", 1, "fourteen chars", None), "display_identity"),
    (lambda: DisplayIdentity("XYM", 1, "", ""), "display_identity"),
    (lambda: DisplayIdentity("XYM", 1, "", "a\nb"), "display_identity"),
    (lambda: DisplayMode(0, 1080, 60000), "display_mode"),
    (lambda: DisplayMode(1920, 1080, 999), "display_mode"),
])
def test_identity_and_mode_refuse(build, code):
    with pytest.raises(ValueError, match=f"^{code}$"):
        build()


@pytest.mark.parametrize("raw", [
    json.dumps(_document(output_id="HDMI-A-3")).encode(),                       # unknown output id
    json.dumps(_document(power=[])).encode(),                                   # empty stack
    json.dumps(_document(power=list(reversed(_document()["power"])))).encode(),   # a timed last request
    json.dumps(_document(power=[_document()["power"][1]] * 2)).encode(),        # duplicate request ids
    json.dumps(_document(extra=1)).encode(),                                    # unknown field
    json.dumps({key: value for key, value in _document().items() if key != "method"}).encode(),   # missing
    json.dumps(_document(change="3")).encode(),
    json.dumps(_document(change=True)).encode(),
    json.dumps(_document(switch_input_on_power_on=1)).encode(),
    CANONICAL[:-1] + b',"change":3}',                                           # duplicate JSON key
    CANONICAL.replace(b'"change":3', b'"change":NaN'),
    CANONICAL + b" " * (OUTPUT_DOCUMENT_BYTES + 1 - len(CANONICAL)),            # past the size
    b"[]",
    b"\xff",
    json.dumps({key: value for key, value in _document().items()
                if key not in ("picture", "picture_change")}).encode(),             # a 1b document
    json.dumps(_document(picture=None, picture_change=None)).encode(),           # F0's null picture
], ids=["unknown-output", "empty-stack", "timed-last", "duplicate-ids", "unknown-field", "missing-field",
        "string-change", "bool-change", "int-switch", "duplicate-key", "nan", "too-large", "not-object",
        "not-utf8", "no-picture-part", "null-picture-part"])
def test_decoding_a_document_refuses(raw):
    with pytest.raises(ValueError, match="^output_document$"):
        decode_output_document(raw)


def test_a_document_at_the_size_limit_still_decodes():
    raw = CANONICAL + b" " * (OUTPUT_DOCUMENT_BYTES - len(CANONICAL))
    assert decode_output_document(raw) == DOCUMENT


def test_decoding_a_report_or_attempt_refuses():
    report = json.loads(encode_output_report(REPORT))
    for broken in ({**report, "answers": ["signal-off", "ddc-ci"]}, {**report, "identity": {"maker": "XYM"}},
                   {**report, "modes": [{"width": 1, "height": 1, "refresh_millihertz": 60000}]},
                   {**report, "in_force": {"request_id": "x", "remaining_seconds": -1}},
                   {key: value for key, value in report.items() if key != "picture"}):   # a 1b (schema 1) report
        with pytest.raises(ValueError, match="^output_report$"):
            decode_output_report(json.dumps(broken).encode())
    attempt = json.loads(encode_power_attempt(ATTEMPT))
    for broken in ({**attempt, "result": "lit"}, {**attempt, "elapsed_ms": -1}, {**attempt, "unknown": 0}):
        with pytest.raises(ValueError, match="^power_attempt$"):
            decode_power_attempt(json.dumps(broken).encode())


# 1c (slice T0): the picture parts of the document and the report.

PICTURE_DOCUMENT = OutputDocument("HDMI-A-1", 3, (STANDING,), BEST_DETECTED, True, False, picture_change=17,
                                  picture=PICTURE)
PICTURE_REPORT = PictureReport(True, False, 17, SettingReport(ActsOn.DISPLAY, 60, WriteResult.TAKEN),
                               SettingReport(ActsOn.PICTURE_ADJUSTMENT, None, None), 16)


def test_a_document_and_a_report_with_picture_parts_round_trip():
    raw = encode_output_document(PICTURE_DOCUMENT)
    assert decode_output_document(raw) == PICTURE_DOCUMENT
    assert b'"value":"as-the-display-has-it"' in raw and b'"value":60' in raw
    report = _report(picture=PICTURE_REPORT)
    assert decode_output_report(encode_output_report(report)) == report
    for result in WriteResult:
        report = _report(picture=PictureReport(False, True, 1, SettingReport(ActsOn.DISPLAY, None, result),
                                               SettingReport(ActsOn.DISPLAY, 0, None), None))
        assert decode_output_report(encode_output_report(report)) == report


def test_a_picture_documents_bytes_are_canonical_whatever_the_field_order():
    canonical = encode_output_document(PICTURE_DOCUMENT)
    parsed = json.loads(canonical)
    picture = parsed["picture"]
    shuffled = {key: parsed[key] for key in reversed(list(parsed))}
    shuffled["picture"] = {key: (dict(reversed(value.items())) if isinstance(value, dict) else value)
                           for key, value in reversed(list(picture.items()))}
    raw = json.dumps(shuffled).encode()
    assert raw != canonical
    assert encode_output_document(decode_output_document(raw)) == canonical


@pytest.mark.parametrize("build, code", [
    (lambda: OutputDocument("HDMI-A-1", 3, (STANDING,), BEST_DETECTED, True, False, 17, None),
     "output_document"),                                                           # change without picture
    (lambda: OutputDocument("HDMI-A-1", 3, (STANDING,), BEST_DETECTED, True, False, None, PICTURE),
     "output_document"),                                                           # picture without change
    (lambda: OutputDocument("HDMI-A-1", 3, (STANDING,), BEST_DETECTED, True, False, picture_change=0,
                            picture=PICTURE), "output_document"),
    (lambda: PictureSetting(ActsOn.DISPLAY, 101), "picture_setting"),
    (lambda: PictureSetting(ActsOn.DISPLAY, -1), "picture_setting"),
    (lambda: PictureSetting(ActsOn.DISPLAY, "50"), "picture_setting"),
    (lambda: PictureSetting(ActsOn.DISPLAY, True), "picture_setting"),
    (lambda: PictureSetting("display", 50), "picture_setting"),
    (lambda: OutputPicture(PICTURE.brightness, PICTURE.contrast, 6550, 22), "output_picture"),
    (lambda: OutputPicture(PICTURE.brightness, PICTURE.contrast, 9400, 22), "output_picture"),
    (lambda: OutputPicture(PICTURE.brightness, PICTURE.contrast, 6500, 27), "output_picture"),
    (lambda: OutputPicture(PICTURE.brightness, PICTURE.contrast, 6500, 2.2), "output_picture"),
    (lambda: SettingReport(ActsOn.PICTURE_ADJUSTMENT, 40, None), "setting_report"),  # a software value as read
    (lambda: SettingReport(ActsOn.PICTURE_ADJUSTMENT, None, WriteResult.TAKEN), "setting_report"),
    (lambda: SettingReport(ActsOn.DISPLAY, 101, None), "setting_report"),
    (lambda: PictureReport(True, False, 17, PICTURE_REPORT.brightness, PICTURE_REPORT.contrast, 0),
     "picture_report"),                                                            # presented_change 0
    (lambda: PictureReport(True, False, 0, PICTURE_REPORT.brightness, PICTURE_REPORT.contrast, None),
     "picture_report"),
    (lambda: PictureReport(1, False, 17, PICTURE_REPORT.brightness, PICTURE_REPORT.contrast, None),
     "picture_report"),
    (lambda: _report(picture={"for_change": 1}), "output_report"),
], ids=["change-without-picture", "picture-without-change", "picture-change-0", "value-101", "value-negative",
        "value-string", "value-bool", "acts-on-string", "kelvin-off-step", "kelvin-past-max", "gamma-27",
        "gamma-float", "read-while-picture-adjustment", "result-while-picture-adjustment", "read-101",
        "presented-0", "for-change-0", "answered-int", "report-picture-not-a-report"])
def test_picture_parts_refuse(build, code):
    with pytest.raises(ValueError, match=f"^{code}$"):
        build()


def test_decoding_picture_parts_refuses():
    document = json.loads(encode_output_document(PICTURE_DOCUMENT))
    for picture in ({**document["picture"], "brightness": {"acts_on": "display", "value": "50"}},
                    {**document["picture"], "contrast": {"acts_on": "panel", "value": 50}},
                    {**document["picture"], "colour_temperature_kelvin": 6550},
                    {**document["picture"], "extra": 1}):
        with pytest.raises(ValueError, match="^output_document$"):
            decode_output_document(json.dumps({**document, "picture": picture}).encode())
    with pytest.raises(ValueError, match="^output_document$"):
        decode_output_document(json.dumps({**document, "picture": None}).encode())   # change without picture
    report = json.loads(encode_output_report(_report(picture=PICTURE_REPORT)))
    for picture in ({**report["picture"], "presented_change": 0},
                    {**report["picture"], "brightness": {"acts_on": "display", "read": 60, "result": "lit"}},
                    {**report["picture"], "contrast": {"acts_on": "picture-adjustment", "read": 3, "result": None}}):
        with pytest.raises(ValueError, match="^output_report$"):
            decode_output_report(json.dumps({**report, "picture": picture}).encode())
