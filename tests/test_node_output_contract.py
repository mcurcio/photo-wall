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
    BEST_DETECTED,
    MAX_POWER_REQUESTS,
    OUTPUT_DOCUMENT_BYTES,
    DisplayIdentity,
    DisplayMode,
    InForce,
    OutputDocument,
    OutputReport,
    Power,
    PowerAttempt,
    PowerMethod,
    PowerRequest,
    PowerResult,
    RequestReason,
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
DOCUMENT = OutputDocument("HDMI-A-1", 3, (TEST_OFF, STANDING), PowerMethod.DDC_CI, True, True)
IDENTITY = DisplayIdentity("XYM", 5475, "MNN", None)
REPORT = OutputReport(
    "HDMI-A-1", True, IDENTITY,
    (DisplayMode(1920, 1080, 60000, preferred=True), DisplayMode(1280, 720, 59940)),
    (PowerMethod.DDC_CI, PowerMethod.SIGNAL_OFF), PowerMethod.DDC_CI, 3, PowerResult.CONFIRMED,
    InForce("req-7", 120))
ATTEMPT = PowerAttempt("HDMI-A-1", 3, "req-7", Power.OFF, PowerMethod.DDC_CI, PowerResult.CONFIRMED, 840)
CANONICAL = (b'{"change":3,"method":"ddc-ci","never_off_on_other_input":true,"output_id":"HDMI-A-1",'
             b'"power":[{"for_seconds":300,"power":"off","reason":"console-test","request_id":"req-7"},'
             b'{"for_seconds":null,"power":"on","reason":"standing","request_id":"standing"}],'
             b'"switch_input_on_power_on":true}')


@pytest.mark.parametrize("value, encode, decode", [
    (DOCUMENT, encode_output_document, decode_output_document),
    (OutputDocument("Virtual-2", 1, (STANDING,), BEST_DETECTED, False, False),
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
    lambda: OutputDocument("HDMI-A-3", 1, (STANDING,), BEST_DETECTED, True, True),
    lambda: OutputDocument("HDMI-A-1", 0, (STANDING,), BEST_DETECTED, True, True),
    lambda: OutputDocument("HDMI-A-1", 1, (), BEST_DETECTED, True, True),
    lambda: OutputDocument("HDMI-A-1", 1, (STANDING, TEST_OFF), BEST_DETECTED, True, True),   # timed last
    lambda: OutputDocument("HDMI-A-1", 1, (TEST_OFF,), BEST_DETECTED, True, True),
    lambda: OutputDocument("HDMI-A-1", 1, (TEST_OFF, TEST_OFF, STANDING), BEST_DETECTED, True, True),
    lambda: OutputDocument("HDMI-A-1", 1, (STANDING,) * 2, BEST_DETECTED, True, True),
    lambda: OutputDocument("HDMI-A-1", 1, tuple(PowerRequest(f"r{index}", Power.ON, RequestReason.STANDING)
                                               for index in range(MAX_POWER_REQUESTS + 1)), BEST_DETECTED, True, True),
    lambda: OutputDocument("HDMI-A-1", 1, (STANDING,), "smart-plug", True, True),
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
], ids=["unknown-output", "empty-stack", "timed-last", "duplicate-ids", "unknown-field", "missing-field",
        "string-change", "bool-change", "int-switch", "duplicate-key", "nan", "too-large", "not-object",
        "not-utf8"])
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
                   {**report, "in_force": {"request_id": "x", "remaining_seconds": -1}}):
        with pytest.raises(ValueError, match="^output_report$"):
            decode_output_report(json.dumps(broken).encode())
    attempt = json.loads(encode_power_attempt(ATTEMPT))
    for broken in ({**attempt, "result": "lit"}, {**attempt, "elapsed_ms": -1}, {**attempt, "unknown": 0}):
        with pytest.raises(ValueError, match="^power_attempt$"):
            decode_power_attempt(json.dumps(broken).encode())
