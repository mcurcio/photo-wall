"""The Output document and report on the wire (roadmap 1b, slice T1; roadmap 2a, schema 3; the
picture split, schema 4; contracts/node_output.py).

Every type round-trips through its codec; a document's bytes are canonical (one encoding however its
input was ordered, so a digest of them is stable); and each refusal the contract names is refused
with the type's own code. Schema 3 (Power and the week): the document's power part is a timetable of
calendar set times with the console test on top, and the largest one still fits the bus's 4096 bytes.
Schema 4 (decision 0022): picture has left both for its own keys (contracts/node_picture.py), so a
document or report that still carries a picture part is refused.
"""
from __future__ import annotations

import itertools
import json

import pytest

from contracts.node_output import (
    BEST_DETECTED,
    MAX_POWER_CHANGES,
    MAX_REQUEST_SECONDS,
    OUTPUT_DOCUMENT_BYTES,
    SCHEMA_MAJOR,
    Darkness,
    DisplayIdentity,
    DisplayMode,
    InForce,
    InForceKind,
    OutputDocument,
    OutputPower,
    OutputReport,
    Power,
    PowerAttempt,
    PowerChange,
    PowerMethod,
    PowerRequest,
    PowerResult,
    decode_output_document,
    decode_output_report,
    decode_power_attempt,
    decode_set_time,
    encode_output_document,
    encode_output_report,
    encode_power_attempt,
    encode_set_time,
    output_document_key,
    output_report_key,
)

TEST_OFF = PowerRequest("req-7", Power.OFF, for_seconds=300)
TIMETABLE = OutputPower(Power.ON, (PowerChange("2026-10-12T22:00:00Z", Power.OFF),
                                   PowerChange("2026-10-13T06:00:00Z", Power.ON)),
                        "2026-10-20T23:00:00Z", Darkness.DISPLAY_OFF, None)
WITH_TEST = OutputPower(Power.ON, TIMETABLE.changes, TIMETABLE.until, Darkness.DISPLAY_OFF, TEST_OFF)
DOCUMENT = OutputDocument("HDMI-A-1", 3, WITH_TEST, PowerMethod.DDC_CI, True, True)
IDENTITY = DisplayIdentity("XYM", 5475, "MNN", None)
TESTING = InForce(InForceKind.TEST, Power.OFF, "req-7", 120, None)
REPORT = OutputReport(
    "HDMI-A-1", True, IDENTITY,
    (DisplayMode(1920, 1080, 60000, preferred=True), DisplayMode(1280, 720, 59940)),
    (PowerMethod.DDC_CI, PowerMethod.SIGNAL_OFF), PowerMethod.DDC_CI, 3, PowerResult.CONFIRMED, TESTING)
ATTEMPT = PowerAttempt("HDMI-A-1", 3, TESTING, PowerMethod.DDC_CI, PowerResult.CONFIRMED, 840)
CANONICAL = (b'{"change":3,"method":"ddc-ci","never_off_on_other_input":true,"output_id":"HDMI-A-1",'
             b'"power":{"changes":[{"at":"2026-10-12T22:00:00Z","power":"off"},'
             b'{"at":"2026-10-13T06:00:00Z","power":"on"}],"darkness":"display-off","now":"on",'
             b'"test":{"for_seconds":300,"power":"off","request_id":"req-7"},"until":"2026-10-20T23:00:00Z"},'
             b'"switch_input_on_power_on":true}')


def test_the_display_line_is_schema_6():
    assert SCHEMA_MAJOR == 6


@pytest.mark.parametrize("value, encode, decode", [
    (DOCUMENT, encode_output_document, decode_output_document),
    (OutputDocument("Virtual-2", 1, TIMETABLE, BEST_DETECTED, False, False),
     encode_output_document, decode_output_document),
    (REPORT, encode_output_report, decode_output_report),
    (OutputReport("HDMI-A-2", False, None, (), (), None, None, None, None),
     encode_output_report, decode_output_report),
    (OutputReport("HDMI-A-2", True, DisplayIdentity("ABC", 0, "", "SN 0042"), (), (PowerMethod.HDMI_CEC,),
                  PowerMethod.HDMI_CEC, 9, PowerResult.ANOTHER_INPUT,
                  InForce(InForceKind.TIMETABLE, Power.OFF, None, None, "2026-10-12T22:00:00Z")),
     encode_output_report, decode_output_report),
    (OutputReport("HDMI-A-2", True, None, (), (), None, 9, None,
                  InForce(InForceKind.TIMETABLE_RAN_OUT, None, None, None, None)),
     encode_output_report, decode_output_report),
    (ATTEMPT, encode_power_attempt, decode_power_attempt),
    (PowerAttempt("HDMI-A-2", 1, InForce(InForceKind.TIMETABLE, Power.ON, None, None, None), None,
                  PowerResult.NOT_SUPPORTED, 0), encode_power_attempt, decode_power_attempt),
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
        shuffled["power"] = dict(reversed(parsed["power"].items()))
        raw = json.dumps(shuffled).encode()
        assert raw != CANONICAL
        assert encode_output_document(decode_output_document(raw)) == CANONICAL


def test_set_times_are_whole_utc_seconds_both_ways():
    assert encode_set_time(1791842400) == "2026-10-12T22:00:00Z"
    assert decode_set_time("2026-10-12T22:00:00Z") == 1791842400
    for bad in ("2026-10-12T22:00:00", "2026-10-12 22:00:00Z", "2026-02-30T22:00:00Z", "2026-10-12T24:00:00Z",
                "2026-10-12T22:00:00.5Z", "", None, 1791842400):
        with pytest.raises(ValueError, match="^set_time$"):
            decode_set_time(bad)
    for bad in (-1, 1.5, True, "1"):
        with pytest.raises(ValueError, match="^set_time$"):
            encode_set_time(bad)


def test_the_largest_document_fits_the_bus():
    """MAX_POWER_CHANGES changes and a test fit OUTPUT_DOCUMENT_BYTES."""
    start = 1791842400
    changes = tuple(PowerChange(encode_set_time(start + 3600 * index), Power.OFF if index % 2 == 0 else Power.ON)
                    for index in range(MAX_POWER_CHANGES))
    power = OutputPower(Power.ON, changes, encode_set_time(start + 3600 * MAX_POWER_CHANGES), Darkness.BLACK_PICTURE,
                        PowerRequest("0" * 64, Power.OFF, MAX_REQUEST_SECONDS))
    document = OutputDocument("Virtual-1", 2**31, power, BEST_DETECTED, True, True)
    raw = encode_output_document(document)
    assert len(raw) <= OUTPUT_DOCUMENT_BYTES
    assert decode_output_document(raw) == document


def test_the_bus_keys():
    assert output_document_key("HDMI-A-1") == "output-HDMI-A-1"
    assert output_report_key("HDMI-A-2") == "output-HDMI-A-2"
    for key in (output_document_key, output_report_key):
        with pytest.raises(ValueError, match="output_id"):
            key("HDMI-A-3")


def _document(**changes) -> dict:
    return {**json.loads(CANONICAL), **changes}


def _power(**changes) -> dict:
    return {**json.loads(CANONICAL)["power"], **changes}


@pytest.mark.parametrize("build", [
    lambda: OutputDocument("HDMI-A-3", 1, TIMETABLE, BEST_DETECTED, True, True),
    lambda: OutputDocument("HDMI-A-1", 0, TIMETABLE, BEST_DETECTED, True, True),
    lambda: OutputDocument("HDMI-A-1", 1, (TEST_OFF,), BEST_DETECTED, True, True),
    lambda: OutputDocument("HDMI-A-1", 1, TIMETABLE, "smart-plug", True, True),
    lambda: OutputDocument("HDMI-A-1", 1, TIMETABLE, BEST_DETECTED, 1, True),
], ids=["unknown-output", "change-0", "a-request-stack", "unknown-method", "int-switch"])
def test_a_document_refuses(build):
    with pytest.raises(ValueError, match="^output_document$"):
        build()


ON_AT_10 = PowerChange("2026-10-12T10:00:00Z", Power.ON)
OFF_AT_11 = PowerChange("2026-10-12T11:00:00Z", Power.OFF)


@pytest.mark.parametrize("build", [
    lambda: OutputPower(Power.ON, (OFF_AT_11, ON_AT_10), "2026-10-20T00:00:00Z", Darkness.DISPLAY_OFF, None),
    lambda: OutputPower(Power.ON, (ON_AT_10, ON_AT_10), "2026-10-20T00:00:00Z", Darkness.DISPLAY_OFF, None),
    lambda: OutputPower(Power.ON, (ON_AT_10, OFF_AT_11), "2026-10-12T11:00:00Z", Darkness.DISPLAY_OFF, None),
    lambda: OutputPower(Power.ON, (), "tomorrow", Darkness.DISPLAY_OFF, None),
    lambda: OutputPower("on", (), "2026-10-20T00:00:00Z", Darkness.DISPLAY_OFF, None),
    lambda: OutputPower(Power.ON, (), "2026-10-20T00:00:00Z", "dark", None),
    lambda: OutputPower(Power.ON, (ON_AT_10,) * (MAX_POWER_CHANGES + 1), "2026-10-20T00:00:00Z",
                        Darkness.DISPLAY_OFF, None),
    lambda: OutputPower(Power.ON, [ON_AT_10], "2026-10-20T00:00:00Z", Darkness.DISPLAY_OFF, None),
], ids=["unsorted", "repeated", "change-at-until", "bad-until", "string-now", "unknown-darkness", "too-many",
        "list"])
def test_a_power_part_refuses(build):
    with pytest.raises(ValueError, match="^output_power$"):
        build()


@pytest.mark.parametrize("build, code", [
    (lambda: PowerRequest("", Power.ON, 60), "power_request"),
    (lambda: PowerRequest("req", "on", 60), "power_request"),
    (lambda: PowerRequest("req", Power.OFF, 0), "power_request"),
    (lambda: PowerRequest("req", Power.OFF, 24 * 3600 + 1), "power_request"),
    (lambda: PowerRequest("req", Power.OFF, None), "power_request"),
    (lambda: PowerChange("2026-10-12T22:00Z", Power.OFF), "power_change"),
    (lambda: PowerChange("2026-10-12T22:00:00Z", "off"), "power_change"),
])
def test_a_request_or_change_refuses(build, code):
    with pytest.raises(ValueError, match=f"^{code}$"):
        build()


@pytest.mark.parametrize("build", [
    lambda: InForce(InForceKind.TEST, Power.OFF, None, 120, None),                       # a test with no id
    lambda: InForce(InForceKind.TEST, Power.OFF, "req-7", None, None),                   # a test with no count
    lambda: InForce(InForceKind.TEST, Power.OFF, "req-7", 120, "2026-10-12T22:00:00Z"),  # a test with a set time
    lambda: InForce(InForceKind.TIMETABLE, Power.OFF, "req-7", None, None),              # timetable with an id
    lambda: InForce(InForceKind.TIMETABLE, Power.OFF, None, 3, None),                    # timetable with a count
    lambda: InForce(InForceKind.TIMETABLE, None, None, None, None),                      # timetable with no power
    lambda: InForce(InForceKind.TIMETABLE, Power.OFF, None, None, "22:00"),              # a bad set time
    lambda: InForce(InForceKind.TIMETABLE_RAN_OUT, Power.ON, None, None, None),          # ran out with a power
    lambda: InForce(InForceKind.TIMETABLE_RAN_OUT, None, None, None, "2026-10-12T22:00:00Z"),
    lambda: InForce("test", Power.OFF, "req-7", 120, None),
], ids=["test-no-id", "test-no-count", "test-since", "timetable-id", "timetable-count", "timetable-no-power",
        "bad-since", "ran-out-power", "ran-out-since", "string-kind"])
def test_an_in_force_of_another_kind_is_refused(build):
    with pytest.raises(ValueError, match="^in_force$"):
        build()


def test_an_attempt_never_carries_out_a_timetable_that_ran_out():
    with pytest.raises(ValueError, match="^power_attempt$"):
        PowerAttempt("HDMI-A-1", 1, InForce(InForceKind.TIMETABLE_RAN_OUT, None, None, None, None), None,
                     PowerResult.NOT_SUPPORTED, 0)


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


_UNSORTED = _power(changes=list(reversed(_power()["changes"])))
_AT_UNTIL = _power(until=_power()["changes"][-1]["at"])


@pytest.mark.parametrize("raw", [
    json.dumps(_document(output_id="HDMI-A-3")).encode(),                       # unknown output id
    json.dumps(_document(power=[])).encode(),                                   # a 1b request stack's shape
    json.dumps(_document(power=_UNSORTED)).encode(),                            # unsorted changes
    json.dumps(_document(power=_AT_UNTIL)).encode(),                            # a change at `until`
    json.dumps(_document(power=_power(until="2026-10-20"))).encode(),           # a bad set time
    json.dumps(_document(power=_power(test={"power": "off", "request_id": "r", "for_seconds": None}))).encode(),
    json.dumps(_document(power=_power(reason="console-test"))).encode(),       # schema 2's field
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
    json.dumps(_document(picture_change=2, picture={"colour_temperature_kelvin": 6500, "gamma_tenths": 22,
                                                    "brightness": {"acts_on": "display", "value": 60},
                                                    "contrast": {"acts_on": "display", "value": 50}})).encode(),
], ids=["unknown-output", "request-stack", "unsorted", "change-at-until", "bad-set-time", "untimed-test",
        "schema-2-reason", "unknown-field", "missing-field", "string-change", "bool-change", "int-switch",
        "duplicate-key", "nan", "too-large", "not-object", "not-utf8", "schema-3-picture-part"])
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
                   {**report, "in_force": {"request_id": "x", "remaining_seconds": -1}},   # schema 2's in-force
                   {**report, "in_force": {**report["in_force"], "kind": "standing"}},
                   {**report, "picture": None}):                     # schema 3's picture part, even empty
        with pytest.raises(ValueError, match="^output_report$"):
            decode_output_report(json.dumps(broken).encode())
    attempt = json.loads(encode_power_attempt(ATTEMPT))
    for broken in ({**attempt, "result": "lit"}, {**attempt, "elapsed_ms": -1}, {**attempt, "unknown": 0},
                   {**attempt, "request_id": "req-7", "power": "off"}):
        with pytest.raises(ValueError, match="^power_attempt$"):
            decode_power_attempt(json.dumps(broken).encode())
