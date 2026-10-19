"""Picture settings on the Display: the display's own and Photo Wall's, each on its own (decision
0022, the picture split, slice S2; roadmap 1c's Live model; owner, 2026-10-12: "i might set hardware
brightness to 46% AND the software brightness to 114%").

Every report travels the real path (the link store's commit and its report judge: `_report` for the
Output report, `_picture_report` for the picture report on `picture-<output>`); every console write
goes through the operator API the Picture tab calls; the documents are the ones the worker's source
projects (`DisplayDocuments`).

(1) Hardware 46 and software 114 in one session, end to end; picture never costs power; (2) a Done
against the previous document is refused; (3) the refusals; (4) "offered" is known only from a
report for the recorded identity; (5) Revert; (6) an abandoned session reverts with no request;
(7) Copy; (8) the picture is never a Position commit; (9) a Display's picture follows it to another
Pi; (10) migration 075's gain, carried through 092. Slice S4: (11) Copy by what each display
offers; (12) a known list is kept; (13) Revert to the Pi's first `before`; (14) "Changed on the
display's buttons". Room light (decision 0028; the print look's sliders and LUT are gone): (17)
the Display's light's bounds; (18) a light edit is live, waits for the Pi, is kept by Done and
recorded in the picture history; (19) Revert puts it back; (20) Done never early on a differing
light.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Final

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from support.position import PORTRAIT_ON_WIDE, save_position
from test_coordination import schedule, setup_players
from test_display_identity import (
    _SEQ,
    ADMIN,
    AUTH,
    EPOCH,
    NO_SERIAL,
    SERIAL_DISPLAY,
    _bind,
    _frame,
    _report,
    _seen,
    _sql,
    enroll_pi,
)
from test_migration_092 import before_092, migrate_092
from test_migration_094 import before_094, migrate_094

from central.app import create_app
from central.coordination import Coordinator
from central.db import DatabaseTransactionClock
from central.displays.model import Readiness
from central.displays.picture import DisplayPicture, presented
from central.infra.display_store import (
    DOCUMENT_STREAM,
    REPORT_STREAM,
    DisplayDocuments,
    DisplayWakes,
    OutputReportJudge,
    frame_readiness_in,
)
from central.infra.node_link_store import PgLinkStores
from contracts.models import Calibration, Plan
from contracts.node_link import Pipe
from contracts.node_output import (
    DisplayIdentity,
    OutputDocument,
    decode_output_document,
    decode_output_report,
    output_document_key,
)
from contracts.node_picture import (
    NEUTRAL_SOFTWARE,
    UPGRADE_DISPLAY_LIGHT,
    Choice,
    DisplayLight,
    HardwareRange,
    HardwareResult,
    HardwareSetting,
    PictureDocument,
    PictureReport,
    RangeKind,
    SignalRange,
    SignalReport,
    SignalResult,
    TestPatternAsk,
    WriteResult,
    decode_picture_document,
    encode_picture_report,
    picture_document_key,
    picture_report_key,
)
from nodeapi.epoch import Token
from nodeapi.pull import Batch, Read

MIGRATION_075 = Path(__file__).parents[1] / "central/migrations/075_display_picture.sql"
MIGRATION_096 = Path(__file__).parents[1] / "central/migrations/096_tuning_history.sql"
MIGRATION_102 = Path(__file__).parents[1] / "central/migrations/102_rectangle_positions.sql"
MIGRATION_104 = Path(__file__).parents[1] / "central/migrations/104_frame_box_positions.sql"
SERIAL = "pi-picture"
SKEWED_CORNERS = [[0.1, 0.05], [0.95, 0.0], [1.0, 1.0], [0.0, 0.9]]
# The test monitor as the Pi asked it: brightness and contrast as levels of 100, a colour preset list.
OFFERED = (HardwareSetting("10", HardwareRange(RangeKind.LEVEL, 100, ()), 60),
           HardwareSetting("12", HardwareRange(RangeKind.LEVEL, 100, ()), 50))


def _read(brightness: int, contrast: int = 50) -> tuple[HardwareSetting, ...]:
    """OFFERED with the display's latest reads of brightness (10) and contrast (12)."""
    return (HardwareSetting("10", HardwareRange(RangeKind.LEVEL, 100, ()), brightness),
            HardwareSetting("12", HardwareRange(RangeKind.LEVEL, 100, ()), contrast))


def _documents(registry, serial: str = SERIAL, output_id: str = "HDMI-A-1") -> tuple[OutputDocument, PictureDocument]:
    """The Output's two documents as the worker's source projects (and records) them now."""
    source = DisplayDocuments(PgLinkStores(registry.db), DisplayWakes(),
                              times=DatabaseTransactionClock()).source(serial, Pipe.FLEET)
    documents = asyncio.run(source.documents(DOCUMENT_STREAM))
    return (decode_output_document(documents[output_document_key(output_id)]),
            decode_picture_document(documents[picture_document_key(output_id)]))


def _picture(registry, serial: str = SERIAL, output_id: str = "HDMI-A-1") -> PictureDocument:
    return _documents(registry, serial, output_id)[1]


_SIGNAL_APPLIED: Final = object()     # `_picture_report`'s default signal: the change's range applied


def _picture_report(registry, *, offered: tuple[HardwareSetting, ...] | None = OFFERED, for_change: int | None = None,
                    results: dict | None = None, presented: int | None = None, serial: str = SERIAL,
                    output_id: str = "HDMI-A-1", identity: DisplayIdentity | None = NO_SERIAL,
                    data: bytes | None = None, signal: SignalReport | None | object = _SIGNAL_APPLIED,
                    test_pattern_shown: TestPatternAsk | None = None) -> None:
    """The Pi's picture report, recorded by Central's link store on the display line's state bucket.
    `signal` by default answers `for_change` (Auto in force, applied), as a Pi on Auto does; None
    with no `for_change`."""
    if signal is _SIGNAL_APPLIED:
        signal = None if for_change is None else SignalReport(for_change, SignalRange.AUTO, SignalResult.APPLIED)
    assert signal is None or isinstance(signal, SignalReport)
    report = PictureReport(output_id, identity, offered, for_change, results or {}, presented, signal,
                           test_pattern_shown)
    token = Token(EPOCH, next(_SEQ))
    read = Read(token, "$KV.state_display." + picture_report_key(output_id), {},
                encode_picture_report(report) if data is None else data)
    stores = PgLinkStores(registry.db, {REPORT_STREAM: OutputReportJudge(DisplayWakes())})
    asyncio.run(stores.store(serial, Pipe.FLEET).commit(REPORT_STREAM, Batch((read,), token, False)))


def _display(registry, player_id: str, output_id: str = "HDMI-A-1") -> dict:
    [row] = _sql(registry, "SELECT d.* FROM output_displays o JOIN displays d ON d.id=o.display_id "
                           "WHERE o.player_id=%s AND o.output_id=%s", (player_id, output_id))
    return row


def _bound(registry, frame_id: str = "kitchen", serial: str = SERIAL, *, offered: bool = True) -> str:
    """A Pi with `frame_id` bound to its HDMI-A-1, the display reported there and, when `offered`,
    its picture list."""
    player = enroll_pi(registry, serial)
    _bind(registry, _frame(registry, frame_id), player, "HDMI-A-1")
    _report(registry, serial, "HDMI-A-1", NO_SERIAL)
    if offered:
        _picture_report(registry, serial=serial)
    return player


def _client(registry) -> TestClient:
    return TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False))


def _begin(client, frame_id: str = "kitchen") -> dict:
    response = client.post(f"/v1/operator/frames/{frame_id}/picture-sessions", headers=AUTH)
    assert response.status_code == 201, response.text
    return response.json()


def _hardware(client, session: dict, code: str, value: int | None, expected: int):
    return client.post(f"/v1/operator/picture-sessions/{session['session_id']}/hardware-edits", headers=AUTH,
                       json={"code": code, "value": value, "expected_sequence": expected})


def _software(client, session: dict, setting: str, value: int, expected: int):
    return client.post(f"/v1/operator/picture-sessions/{session['session_id']}/software-edits", headers=AUTH,
                       json={"setting": setting, "value": value, "expected_sequence": expected})


def _view(client, frame_id: str = "kitchen") -> dict:
    response = client.get(f"/v1/operator/frames/{frame_id}/picture", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def _done(client, session: dict):
    return client.post(f"/v1/operator/picture-sessions/{session['session_id']}/done", headers=AUTH)


def test_hardware_46_and_software_114_are_set_apart_end_to_end_and_never_cost_power(registry):
    """(1) Acceptance 1. Mutation probe (primary): include the picture in the Output document's
    digest, or raise `change` on a picture write -> the power change rises with the edit."""
    player = _bound(registry)
    with _client(registry) as client:
        power_before, picture_before = _documents(registry)
        assert (picture_before.hardware, picture_before.software.contrast_percent) == ({}, 100)
        session = _begin(client)
        assert _hardware(client, session, "10", 46, 1).json() == {"sequence": 2}
        assert _software(client, session, "contrast", 114, 2).json() == {"sequence": 3}
        power, picture = _documents(registry)
        assert dict(picture.hardware) == {"10": 46} and picture.software.contrast_percent == 114
        assert power.change == power_before.change                     # picture never costs power
        assert picture.picture_change > picture_before.picture_change

        settings = client.get("/v1/operator/frames/kitchen/power", headers=AUTH).json()
        assert client.put(f"/v1/operator/displays/{settings['display_id']}/power-settings", headers=AUTH, json={
            "power_method": "signal-off", "switch_input_on_power_on": True,
            "never_off_on_other_input": False}).status_code == 200
        powered, unmoved = _documents(registry)
        assert (powered.change, unmoved.picture_change) == (power.change + 1, picture.picture_change)
        assert _documents(registry) == (powered, unmoved)                # unchanged facts: neither number moves

        early = _done(client, session)
        assert (early.status_code, early.json()) == (409, {"error": "not_presented_yet"})
        assert _view(client)["status"] == "waiting"
        _picture_report(registry, for_change=picture.picture_change, presented=picture.picture_change,
                        offered=_read(46), results={"10": HardwareResult(WriteResult.TAKEN, 60, 46)})
        view = _view(client)
        assert view["status"] == "shown" and view["offered_known"] is True
        brightness = next(row for row in view["hardware"] if row["code"] == "10")
        assert brightness == {"code": "10", "name": "Brightness", "value": 46,
                              "offered": {"range": {"kind": "level", "maximum": 100, "choices": []}, "current": 46},
                              "result": {"result": "taken", "before": 60, "after": 46},
                              "changed_on_buttons": False}
        contrast = next(row for row in view["hardware"] if row["code"] == "12")
        assert (contrast["value"], contrast["result"]) == (None, None)  # unset: as the display has it
        assert view["software"] == {"contrast_percent": 114, "colour_temperature_kelvin": 6500, "gamma_tenths": 22}
        assert view["light"] == {"matched_lux": 1000.0, "black": 0.0003}
        assert _done(client, session).status_code == 204
    stored = _display(registry, player)
    assert (stored["hardware_picture"], stored["software_contrast_percent"]) == ({"10": 46}, 114)
    assert _sql(registry, "SELECT id FROM picture_sessions") == []


def test_done_against_the_previous_document_is_refused(registry):
    """(2) The report answers the document the source projected before the latest hardware edit.
    Mutation probe: `presented` ignores hardware equality -> that Done is taken."""
    _bound(registry)
    with _client(registry) as client:
        session = _begin(client)
        _hardware(client, session, "10", 46, 1)
        shown = _picture(registry)
        _picture_report(registry, for_change=shown.picture_change, presented=shown.picture_change,
                        results={"10": HardwareResult(WriteResult.TAKEN, 60, 46)})
        assert _hardware(client, session, "10", 50, 2).status_code == 200     # not projected yet
        stale = _done(client, session)
        assert (stale.status_code, stale.json()) == (409, {"error": "not_presented_yet"})
        assert _view(client)["status"] == "waiting"


def test_an_edit_is_refused_by_what_the_display_offers_and_photo_walls_ranges(registry):
    """(3) Acceptance 2. Mutation probe: `edit_refusal` accepts value <= maximum + 1 -> 101 is taken."""
    _bound(registry)
    with _client(registry) as client:
        session = _begin(client)
        refusals = [
            (_hardware(client, session, "D6", 1, 1), 409, "not_offered"),       # power: off the picture list
            (_hardware(client, session, "10", 101, 1), 422, "out_of_range"),    # past the display's 100
            (_hardware(client, session, "16", 40, 1), 409, "not_offered"),      # on the list, not offered
            (_software(client, session, "contrast", 151, 1), 422, "out_of_range"),
            (_software(client, session, "gamma", 27, 1), 422, "out_of_range"),
        ]
        for answer, status, code in refusals:
            assert (answer.status_code, answer.json()) == (status, {"error": code})
        assert _hardware(client, session, "16", None, 1).json() == {"sequence": 2}   # handing back is fine
        assert _hardware(client, session, "10", 100, 2).json() == {"sequence": 3}
        stale = _software(client, session, "contrast", 30, 2)
        assert (stale.status_code, stale.json()) == (409, {"error": "stale_sequence"})
        assert _view(client)["session"]["sequence"] == 3


def test_what_a_display_offers_is_known_only_from_a_report_for_its_recorded_identity(registry):
    """(4) Acceptance 3: no picture report, a report naming another display, and a report that does
    not know the list yet all leave the list unknown, and a hardware value cannot be set."""
    _bound(registry, offered=False)
    with _client(registry) as client:
        assert (_view(client)["offered_known"], _view(client)["hardware"]) == (False, [])
        _picture_report(registry, identity=SERIAL_DISPLAY)
        assert _view(client)["offered_known"] is False
        _picture_report(registry, offered=None)
        assert _view(client)["offered_known"] is False
        session = _begin(client)
        refused = _hardware(client, session, "10", 46, 1)
        assert (refused.status_code, refused.json()) == (409, {"error": "not_offered"})
        _picture_report(registry)
        view = _view(client)
        assert view["offered_known"] is True
        assert [(row["code"], row["name"]) for row in view["hardware"]] == [("10", "Brightness"), ("12", "Contrast")]
        assert _hardware(client, session, "10", 46, 1).status_code == 200


def test_revert_puts_back_the_stored_values(registry):
    """(5) Both groups go back to what is stored; a code set only in the session goes from the
    document."""
    player = _bound(registry)
    _sql(registry, "UPDATE displays SET hardware_picture='{\"12\": 30}', software_contrast_percent=120")
    with _client(registry) as client:
        session = _begin(client)
        _hardware(client, session, "10", 46, 1)
        _hardware(client, session, "12", 70, 2)
        _software(client, session, "contrast", 80, 3)
        _software(client, session, "colour-temperature", 3200, 4)
        assert dict(_picture(registry).hardware) == {"10": 46, "12": 70}
        assert client.delete(f"/v1/operator/picture-sessions/{session['session_id']}",
                             headers=AUTH).status_code == 204
        gone = _software(client, session, "contrast", 20, 5)
        assert (gone.status_code, gone.json()) == (404, {"error": "unknown_session"})
    stored = _display(registry, player)
    assert (stored["hardware_picture"], stored["software_contrast_percent"],
            stored["colour_temperature_kelvin"]) == ({"12": 30}, 120, 6500)
    reverted = _picture(registry)
    assert (dict(reverted.hardware), reverted.software.contrast_percent) == ({"12": 30}, 120)


def test_an_abandoned_session_reverts_with_no_request(registry):
    """(6) Past its idle deadline the document shows the stored values with no request made; the
    next session folds it first. Mutation probe: a projection that ignores `idle_until` -> the draft
    stays in the document."""
    player = _bound(registry)
    with _client(registry) as client:
        session = _begin(client)
        _software(client, session, "contrast", 90, 1)
        assert _picture(registry).software.contrast_percent == 90
        _sql(registry, "UPDATE picture_sessions SET idle_until = EXTRACT(EPOCH FROM clock_timestamp()) - 1")
        assert _picture(registry).software.contrast_percent == 100
        ended = client.post(f"/v1/operator/picture-sessions/{session['session_id']}/keepalive", headers=AUTH)
        assert (ended.status_code, ended.json()) == (410, {"error": "session_ended"})
        _begin(client)
    assert _display(registry, player)["software_contrast_percent"] == 100


def test_the_sources_wait_ends_at_a_sessions_idle_deadline(registry):
    """(6, the wake) The source's changed() returns at the session's idle deadline, with no wake."""
    _bound(registry)
    with _client(registry) as client:
        _begin(client)
    _sql(registry, "UPDATE picture_sessions SET idle_until = EXTRACT(EPOCH FROM clock_timestamp()) + 1")
    source = DisplayDocuments(PgLinkStores(registry.db), DisplayWakes(),
                              times=DatabaseTransactionClock()).source(SERIAL, Pipe.FLEET)

    async def ended() -> None:
        await asyncio.wait_for(source.changed(), 5)
    asyncio.run(ended())


def test_copy_carries_the_software_values_names_the_hardware_skipped_and_never_position(registry):
    """(7) Copy carries all four software values; with neither display's list known, each hardware
    value set on the source is named as skipped and the target's own stay."""
    pi_a, pi_b = enroll_pi(registry, "pi-a"), enroll_pi(registry, "pi-b")
    _bind(registry, _frame(registry, "a"), pi_a, "HDMI-A-1")
    _bind(registry, _frame(registry, "b"), pi_b, "HDMI-A-1")
    _bind(registry, _frame(registry, "c"), pi_b, "HDMI-A-2")
    _frame(registry, "d")                                                  # never bound: no Display
    _report(registry, "pi-a", "HDMI-A-1", SERIAL_DISPLAY)
    _report(registry, "pi-b", "HDMI-A-1", NO_SERIAL)
    save_position(registry, "b", PORTRAIT_ON_WIDE)
    _sql(registry, "UPDATE displays SET hardware_picture='{\"12\": 35, \"10\": 65}', matched_lux=2500, "
                   "software_contrast_percent=70, colour_temperature_kelvin=5000, gamma_tenths=20 "
                   "WHERE serial IS NOT NULL")
    _sql(registry, "UPDATE displays SET hardware_picture='{\"10\": 20}' WHERE serial IS NULL")
    # A's display moves to C's port: C is fed by A's own Display.
    _report(registry, "pi-a", "HDMI-A-1", None, connected=False)
    _report(registry, "pi-b", "HDMI-A-2", SERIAL_DISPLAY)
    before = _sql(registry, "SELECT calibration, profile, position_generation FROM frames WHERE id='b'")
    with _client(registry) as client:
        copied = client.post("/v1/operator/frames/a/picture-copies", headers=AUTH, json={"to_frames": ["b", "c", "d"]})
        assert copied.status_code == 200, copied.text
        assert copied.json() == [{"frame_id": "b", "outcome": "copied", "hardware_skipped": ["10", "12"]},
                                 {"frame_id": "c", "outcome": "same-display", "hardware_skipped": []},
                                 {"frame_id": "d", "outcome": "no-display", "hardware_skipped": []}]
        unknown = client.post("/v1/operator/frames/a/picture-copies", headers=AUTH, json={"to_frames": ["zz"]})
        assert (unknown.status_code, unknown.json()) == (404, {"error": "unknown_frame"})
    target = _display(registry, pi_b, "HDMI-A-1")
    assert (target["matched_lux"], target["software_contrast_percent"],
            target["colour_temperature_kelvin"], target["gamma_tenths"]) == (2500.0, 70, 5000, 20)
    assert target["hardware_picture"] == {"10": 20}
    assert _sql(registry, "SELECT calibration, profile, position_generation FROM frames WHERE id='b'") == before


def test_a_picture_edit_and_done_are_never_a_position_commit(registry):
    """(8) Mutation probe: an edit raising the position revision -> the revisions move."""
    player = _bound(registry)
    save_position(registry, "kitchen", PORTRAIT_ON_WIDE)
    columns = "SELECT calibration, configuration_revision, generation, position_generation FROM frames"
    before = _sql(registry, columns)
    with _client(registry) as client:
        session = _begin(client)
        _software(client, session, "contrast", 130, 1)
        latest = _picture(registry)
        _picture_report(registry, for_change=latest.picture_change, presented=latest.picture_change)
        assert _done(client, session).status_code == 204
    assert _sql(registry, columns) == before
    assert next(frame for frame in registry.inventory().frames if frame.id == "kitchen").readiness is Readiness.READY
    assert _display(registry, player)["software_contrast_percent"] == 130


def test_a_displays_picture_follows_it_to_another_pis_output(registry):
    """(9) Serial-keyed: the picture is the Display's, not the Output's; an Output with no Display
    seen gets a new Display's picture."""
    pi_a, pi_b = enroll_pi(registry, "pi-a"), enroll_pi(registry, "pi-b")
    _report(registry, "pi-a", "HDMI-A-1", SERIAL_DISPLAY)
    display_id = _seen(registry, pi_a, "HDMI-A-1")["display_id"]
    _sql(registry, "UPDATE displays SET hardware_picture='{\"10\": 46}', software_contrast_percent=114, "
                   "gamma_tenths=24 WHERE id=%s", (display_id,))
    _report(registry, "pi-a", "HDMI-A-1", None, connected=False)
    assert _picture(registry, "pi-b", "HDMI-A-2").software.gamma_tenths == 22     # nothing seen there yet
    _report(registry, "pi-b", "HDMI-A-2", SERIAL_DISPLAY)

    assert _seen(registry, pi_b, "HDMI-A-2")["display_id"] == display_id
    moved = _picture(registry, "pi-b", "HDMI-A-2")
    assert (dict(moved.hardware), moved.software.contrast_percent, moved.software.gamma_tenths) == ({"10": 46}, 114, 24)


def test_a_picture_report_that_does_not_decode_or_names_another_output_is_skipped(registry):
    _bound(registry, offered=False)
    _picture_report(registry, data=b'{"output_id":"HDMI-A-1"}')
    _picture_report(registry, output_id="HDMI-A-2", data=encode_picture_report(
        PictureReport("HDMI-A-1", NO_SERIAL, OFFERED, None, {}, None)))
    assert _sql(registry, "SELECT output_id FROM picture_reports") == []
    _picture_report(registry)
    assert [row["output_id"] for row in _sql(registry, "SELECT output_id FROM picture_reports")] == ["HDMI-A-1"]


def test_a_session_needs_a_bound_frame_with_a_display(registry):
    enroll_pi(registry, SERIAL)
    _frame(registry, "loose")
    player = enroll_pi(registry, "pi-other")
    _bind(registry, _frame(registry, "kitchen"), player, "HDMI-A-1")
    with _client(registry) as client:
        assert client.post("/v1/operator/frames/nowhere/picture-sessions", headers=AUTH).json() == {
            "error": "unknown_frame"}
        assert client.post("/v1/operator/frames/loose/picture-sessions", headers=AUTH).json() == {
            "error": "frame_unbound"}
        assert client.post("/v1/operator/frames/kitchen/picture-sessions", headers=AUTH).json() == {
            "error": "no_display"}
        picture = _view(client, "loose")
        assert (picture["status"], picture["display_id"], picture["session"]) == ("no-display", None, None)
        assert client.post("/v1/operator/frames/kitchen/picture-sessions").status_code == 401


def _revert(client, session: dict) -> None:
    assert client.delete(f"/v1/operator/picture-sessions/{session['session_id']}", headers=AUTH).status_code == 204


def test_copy_carries_each_hardware_value_the_target_offers_the_same_way(registry):
    """(11) 10 = 46 of 100 goes to a display counting 10 of 255 as 117; 14 = 5 "6500 K" is skipped
    where 5 is "sRGB"; the target's own 12 stays."""
    pi_a, pi_b = enroll_pi(registry, "pi-a"), enroll_pi(registry, "pi-b")
    _bind(registry, _frame(registry, "a"), pi_a, "HDMI-A-1")
    _bind(registry, _frame(registry, "b"), pi_b, "HDMI-A-1")
    _report(registry, "pi-a", "HDMI-A-1", SERIAL_DISPLAY)
    _report(registry, "pi-b", "HDMI-A-1", NO_SERIAL)
    preset = HardwareRange(RangeKind.CHOICE, None, (Choice(4, "5000 K"), Choice(5, "6500 K")))
    _picture_report(registry, serial="pi-a", identity=SERIAL_DISPLAY, offered=(
        HardwareSetting("10", HardwareRange(RangeKind.LEVEL, 100, ()), 46), HardwareSetting("14", preset, 5)))
    _picture_report(registry, serial="pi-b", offered=(
        HardwareSetting("10", HardwareRange(RangeKind.LEVEL, 255, ()), 0),
        HardwareSetting("12", HardwareRange(RangeKind.LEVEL, 100, ()), 30),
        HardwareSetting("14", HardwareRange(RangeKind.CHOICE, None, (Choice(5, "sRGB"),)), 5)))
    _sql(registry, "UPDATE displays SET hardware_picture='{\"10\": 46, \"14\": 5}', software_contrast_percent=114 "
                   "WHERE serial IS NOT NULL")
    _sql(registry, "UPDATE displays SET hardware_picture='{\"12\": 30}' WHERE serial IS NULL")
    with _client(registry) as client:
        copied = client.post("/v1/operator/frames/a/picture-copies", headers=AUTH, json={"to_frames": ["b"]})
        assert copied.json() == [{"frame_id": "b", "outcome": "copied", "hardware_skipped": ["14"]}]
    target = _display(registry, pi_b)
    assert (target["hardware_picture"], target["software_contrast_percent"]) == ({"10": 117, "12": 30}, 114)
    assert dict(_picture(registry, "pi-b").hardware) == {"10": 117, "12": 30}


def test_a_report_that_does_not_know_the_list_yet_keeps_the_known_list_of_the_same_display(registry):
    """(12) The Pi restarted and has not asked yet: the tab keeps the list. Mutation probe:
    `kept_offered` lets None through -> offered_known turns False."""
    _bound(registry)
    with _client(registry) as client:
        _picture_report(registry, offered=None)
        view = _view(client)
        assert view["offered_known"] is True and [row["code"] for row in view["hardware"]] == ["10", "12"]
        _picture_report(registry, offered=None, identity=SERIAL_DISPLAY)    # another display there now
        _report(registry, SERIAL, "HDMI-A-1", SERIAL_DISPLAY)
        assert _view(client)["offered_known"] is False
        _report(registry, SERIAL, "HDMI-A-1", NO_SERIAL)
        _picture_report(registry, offered=None)                              # the first display is back
        assert _view(client)["offered_known"] is False                       # its list was not kept


def test_revert_puts_a_code_set_only_in_the_session_back_to_the_pis_first_before(registry):
    """(13) The Pi read 60 before the session's first write of 10: Revert puts the display back to
    60 once, and 10 stays "as the display has it" (0022: Revert puts back the stored values; the fix
    pass: a one-time instruction, never a stored value); the second write's `before` (46) does not
    replace it; a report for the change the session began at does not count. Once the Pi answers
    the document that puts it back, the document carries the stored values alone. Mutation probes:
    `with_befores` overwrites -> 60 becomes 46; Revert stores `reverted` -> 10 = 60 is kept set."""
    player = _bound(registry)
    began = _picture(registry).picture_change
    with _client(registry) as client:
        session = _begin(client)
        _picture_report(registry, for_change=began, results={"12": HardwareResult(WriteResult.TAKEN, 20, 50)})
        _hardware(client, session, "10", 46, 1)
        first = _picture(registry).picture_change
        _picture_report(registry, for_change=first, results={"10": HardwareResult(WriteResult.TAKEN, 60, 46)})
        _hardware(client, session, "10", 50, 2)
        _hardware(client, session, "12", 70, 3)
        second = _picture(registry).picture_change
        _picture_report(registry, for_change=second, results={"10": HardwareResult(WriteResult.TAKEN, 46, 50)})
        _revert(client, session)
        assert _display(registry, player)["hardware_picture"] == {}        # nothing is set by a Revert
        back = _picture(registry)
        assert dict(back.hardware) == {"10": 60}                          # 12: no before after the session began
        assert next(row for row in _view(client)["hardware"] if row["code"] == "10")["value"] is None
        _picture_report(registry, for_change=second)                     # an older change: still waits
        assert dict(_picture(registry).hardware) == {"10": 60}
        _picture_report(registry, for_change=back.picture_change, results={"10": HardwareResult(WriteResult.TAKEN, 50, 60)})
        assert dict(_picture(registry).hardware) == {}                    # done once: the stored values alone
    assert _display(registry, player)["hardware_picture"] == {}


def test_a_value_changed_on_the_displays_buttons_is_shown_and_not_written_back(registry):
    """(14) The main loop's addition (2026-10-12): the Pi's re-read says 40 where Photo Wall set 46 ->
    the row says so, with no new picture change; not while the Pi has not carried out the value in
    force (E-PS-S4's gate: a report answering the document of 46 says nothing of a draft of 50),
    nor when the display did not take the write. Mutation probe: `hardware_answered` ignores the
    document's hardware -> the draft's 50 shows change 46's result and "changed"."""
    _bound(registry)
    _sql(registry, "UPDATE displays SET hardware_picture='{\"10\": 46}'")
    shown = _picture(registry)

    def brightness(client) -> dict:
        return next(row for row in _view(client)["hardware"] if row["code"] == "10")
    with _client(registry) as client:
        _picture_report(registry, for_change=shown.picture_change, presented=shown.picture_change,
                        offered=_read(46), results={"10": HardwareResult(WriteResult.TAKEN, 60, 46)})
        assert brightness(client)["changed_on_buttons"] is False
        _picture_report(registry, for_change=shown.picture_change, presented=shown.picture_change,
                        offered=_read(40), results={"10": HardwareResult(WriteResult.TAKEN, 60, 46)})
        assert brightness(client)["changed_on_buttons"] is True and brightness(client)["offered"]["current"] == 40
        assert _picture(registry).picture_change == shown.picture_change     # not written back
        _picture_report(registry, for_change=shown.picture_change, presented=shown.picture_change,
                        offered=_read(60), results={"10": HardwareResult(WriteResult.NOT_TAKEN, 60, 60)})
        assert brightness(client)["changed_on_buttons"] is False             # "the display did not take it"
        _picture_report(registry, for_change=shown.picture_change, presented=shown.picture_change,
                        offered=_read(46), results={"10": HardwareResult(WriteResult.TAKEN, 60, 46)})
        session = _begin(client)
        _hardware(client, session, "10", 50, 1)                              # not projected yet
        assert (brightness(client)["changed_on_buttons"], brightness(client)["result"]) == (False, None)


# --- Migration 075, carried through 092 --------------------------------------------------------------

def _before_075(registry) -> None:
    """The database as 1b's build left it: 094 and 092 undone, then no picture columns, the preview
    columns back."""
    before_094(registry)
    before_092(registry)
    _sql(registry, "DROP TABLE picture_sessions")
    _sql(registry, "ALTER TABLE displays DROP COLUMN brightness_acts_on, DROP COLUMN brightness, "
                   "DROP COLUMN contrast_acts_on, DROP COLUMN contrast, DROP COLUMN colour_temperature_kelvin, "
                   "DROP COLUMN gamma_tenths")
    _sql(registry, "ALTER TABLE output_documents DROP COLUMN picture_change, DROP COLUMN picture_digest")
    _sql(registry, "ALTER TABLE frames ADD COLUMN preview JSONB, ADD COLUMN preview_expires DOUBLE PRECISION")


def _migrate(registry) -> None:
    """075, then 092, then 094, on the database `_before_075` left, then 096's own statement for the
    session column (the table `_before_075` dropped came back without it; 096's history tables
    were never dropped)."""
    with registry.db.transaction() as conn:
        conn.execute(MIGRATION_075.read_text())
    migrate_092(registry)
    migrate_094(registry)
    statement, = (part for part in MIGRATION_096.read_text().split(";") if "ALTER TABLE picture_sessions" in part)
    _sql(registry, statement)


def _stored_1b(registry, frame_id: str, *, gain: float, rotation: int = 0, corners=None) -> None:
    calibration = {"revision": 2, "rotation": rotation, "corners": corners or [[0, 0], [1, 0], [1, 1], [0, 1]],
                   "crop": [0.1, 0, 1, 1], "gain": gain}
    _sql(registry, "UPDATE frames SET calibration=%s, position_generation=generation WHERE id=%s",
         (Jsonb(calibration), frame_id))


def _frame_row(registry, frame_id: str):
    return next(frame for frame in registry.inventory().frames if frame.id == frame_id)


def test_075_then_092_move_a_frames_gain_to_its_displays_software_brightness(registry):
    """(10) 1b's gain 0.8 became 1c's picture adjustment 40 %, and 092 makes it 80 %; gain 1.0 stays
    unset, so 100 %. Mutation probe: drop 092's doubling -> 40 %."""
    player = enroll_pi(registry, SERIAL)
    for frame_id, output_id in (("bright", "HDMI-A-1"), ("plain", "HDMI-A-2")):
        _bind(registry, _frame(registry, frame_id), player, output_id)
        _report(registry, SERIAL, output_id, NO_SERIAL)
    _before_075(registry)
    _stored_1b(registry, "bright", gain=0.8)
    _stored_1b(registry, "plain", gain=1.0)
    _migrate(registry)

    bright, plain = _display(registry, player, "HDMI-A-1"), _display(registry, player, "HDMI-A-2")
    assert (bright["software_brightness_percent"], bright["hardware_picture"]) == (80, {})
    assert (plain["software_brightness_percent"], plain["software_contrast_percent"]) == (100, 100)
    assert (bright["colour_temperature_kelvin"], bright["gamma_tenths"]) == (6500, 22)


def test_075_moves_every_frame_to_cover_at_angle_0_and_not_ready(registry):
    """Owner q1 and his rotation answer (2026-10-10, "Every frame should reset to 0 rotation"): a
    Frame stored with corners C and rotation 90 is stored as Cover with C as its opening at angle 0
    (075's JSON, asserted as JSON: today's models no longer read Cover, migration 102 converts it);
    its revision and configuration revision rise; trim and gain are gone; it is not ready until its
    Position is saved again. Mutation probe: drop the readiness statement -> the Frame stays ready."""
    _bound(registry, offered=False)
    _before_075(registry)
    _stored_1b(registry, "kitchen", gain=0.8, rotation=90, corners=SKEWED_CORNERS)
    _sql(registry, "UPDATE frames SET preview=calibration, preview_expires=2000")
    before = _sql(registry, "SELECT configuration_revision FROM frames")[0]["configuration_revision"]
    # Positioned before 075 (the readiness rule's own input; the 1b record no longer parses).
    assert _sql(registry, "SELECT position_generation = generation AS positioned FROM frames")[0]["positioned"]
    _migrate(registry)

    [row] = _sql(registry, "SELECT calibration, configuration_revision FROM frames")
    assert row["calibration"] == {"revision": 3, "fit": {"mode": "cover", "corners": SKEWED_CORNERS, "rotation": 0}}
    assert row["configuration_revision"] == before + 1
    with registry.db.transaction() as conn:
        assert frame_readiness_in(conn, "kitchen") is Readiness.POSITION_NEEDED
    assert not _sql(registry, "SELECT 1 FROM information_schema.columns WHERE table_name='frames' "
                              "AND column_name IN ('preview', 'preview_expires')")


STORED_1B = {"revision": 2, "rotation": 90, "corners": SKEWED_CORNERS, "crop": [0.1, 0, 1, 1], "gain": 1.0}


def _bindings_as_1b(registry, table: str, column: str) -> int:
    """Every binding in `table`.`column` carries a Position as 1b stored it; the rows rewritten."""
    rows = _sql(registry, f"SELECT ctid, {column} AS body FROM {table}")  # noqa: S608
    for row in rows:
        body = {**row["body"], "bindings": [{**binding, "calibration": STORED_1B}
                                            for binding in row["body"]["bindings"]]}
        _sql(registry, f"UPDATE {table} SET {column}=%s WHERE ctid=%s", (Jsonb(body), row["ctid"]))  # noqa: S608
    return len(rows)


def test_075_rewrites_the_stored_configurations_and_offers_so_central_still_plans(registry):
    """A Player's last configuration and its plan offers hold copies of the Frame's Position; 075
    rewrites them to 1c's Cover (asserted as JSON), 102 to a rectangle and 104 to a box, after which
    today's strict models read them, so Central hands the Player a configuration and plans. Mutation probe:
    drop the two binding rewrites -> `configuration` raises."""
    [player] = setup_players(registry, 1)
    coordinator = Coordinator(registry.db, registry.clock)
    epoch = player["authority_epoch"]
    coordinator.configuration(player["player_id"], epoch)
    schedule(coordinator, ["frame-0"])
    _before_075(registry)
    _stored_1b(registry, "frame-0", gain=1.0, rotation=90, corners=SKEWED_CORNERS)
    assert _bindings_as_1b(registry, "player_configurations", "configuration") == 1
    assert _bindings_as_1b(registry, "plan_offers", "manifest") >= 1
    _migrate(registry)
    cover = {"mode": "cover", "corners": SKEWED_CORNERS, "rotation": 0}
    [stored] = _sql(registry, "SELECT configuration FROM player_configurations")
    assert [binding["calibration"]["fit"] for binding in stored["configuration"]["bindings"]] == [cover]
    # 102 and 104 again on a database that already has them: 102's one column comes off first.
    _sql(registry, "ALTER TABLE position_entries DROP COLUMN turn")
    with registry.db.transaction() as conn:
        conn.execute(MIGRATION_102.read_text())
    with registry.db.transaction() as conn:
        conn.execute(MIGRATION_104.read_text())

    configuration = coordinator.configuration(player["player_id"], epoch)
    [binding] = configuration.bindings
    [frame] = _sql(registry, "SELECT calibration FROM frames WHERE id='frame-0'")
    assert binding.calibration == Calibration.model_validate(frame["calibration"])
    assert binding.calibration.fit.mode == "box"
    coordinator.advance()
    for row in _sql(registry, "SELECT manifest FROM plan_offers"):
        Plan.model_validate(row["manifest"])


def test_a_1b_report_and_document_still_answer_after_075_and_092(registry):
    """A Pi's report and Central's document as 1b stored them answer the Power and Picture tabs after
    075 and 092, before the Pi reports again; the picture numbering carries (075's 1, then the
    Display's own). Mutation probe: drop 092's report statement -> the stored report no longer decodes."""
    _bound(registry, offered=False)
    _documents(registry)
    _before_075(registry)
    _sql(registry, "UPDATE output_documents SET document = document - 'picture' - 'picture_change'")
    _migrate(registry)

    with _client(registry) as client:
        assert client.get("/v1/operator/frames/kitchen/power", headers=AUTH).status_code == 200
        assert client.get("/v1/operator/frames/kitchen/picture", headers=AUTH).status_code == 200
    [seen] = _sql(registry, "SELECT report FROM output_displays")
    assert decode_output_report(json.dumps(seen["report"]).encode()).identity == NO_SERIAL
    assert _picture(registry).picture_change == 2


def test_a_display_that_is_away_keeps_its_list_and_takes_edits_until_it_is_back(registry):
    """(15) The fix pass: the cable pulled (or a standby that drops hot-plug), the Pi reports no
    identity and no list; what the display offered at its last check is kept, the rows keep their
    ranges, nothing says "changed on the display's buttons", and an edit is taken (it applies when
    the display is connected). Mutation probe: `kept_report` keeps a list only for an equal identity
    -> offered_known turns False and the edit is refused not_offered."""
    _bound(registry)
    _sql(registry, "UPDATE displays SET hardware_picture='{\"10\": 46}'")
    with _client(registry) as client:
        _report(registry, SERIAL, "HDMI-A-1", None, connected=False)
        _picture_report(registry, offered=None, identity=None)
        view = _view(client)
        assert (view["connected"], view["offered_known"]) == (False, True)
        brightness = next(row for row in view["hardware"] if row["code"] == "10")
        assert brightness["offered"]["range"]["maximum"] == 100 and brightness["changed_on_buttons"] is False
        session = _begin(client)
        assert _hardware(client, session, "10", 30, 1).json() == {"sequence": 2}


def test_a_display_that_offers_nothing_is_known_not_checking(registry):
    """(16) An empty known list ((): asked, offers none) is known, with no rows, never "Checking".
    Mutation probe: offered_known = bool(offered) -> False."""
    _bound(registry, offered=False)
    with _client(registry) as client:
        _picture_report(registry, offered=())
        assert (_view(client)["offered_known"], _view(client)["hardware"]) == (True, [])


# --- The Display's light (decision 0028) ------------------------------------------------------------

def _light(client, session: dict, matched_lux, black, expected: int):
    return client.post(f"/v1/operator/picture-sessions/{session['session_id']}/light-edits", headers=AUTH,
                       json={"matched_lux": matched_lux, "black": black, "expected_sequence": expected})


def test_the_displays_light_is_taken_within_its_ranges(registry):
    """(17) Both bounds taken, one past either refused 422, a stale sequence 409; the old sliders
    and the LUT edit are gone."""
    _bound(registry)
    with _client(registry) as client:
        session = _begin(client)
        for lux, black in ((9.9, 0.001), (20001, 0.001), (500, 0.06), (500, 0)):
            assert _light(client, session, lux, black, 1).status_code == 422, (lux, black)
        assert _light(client, session, 10, 0.00001, 1).json() == {"sequence": 2}
        assert _light(client, session, 20000, 0.05, 2).json() == {"sequence": 3}
        stale = _light(client, session, 800, 0.001, 2)
        assert (stale.status_code, stale.json()) == (409, {"error": "stale_sequence"})
        for setting in ("brightness", "saturation", "highlight-rolloff", "black-floor", "lut-amount"):
            assert _software(client, session, setting, 100, 3).status_code == 422, setting
        assert client.post(f"/v1/operator/picture-sessions/{session['session_id']}/lut-edits", headers=AUTH,
                           json={"profile_sha256": None, "expected_sequence": 3}).status_code in (404, 405)


def test_a_light_edit_is_live_waits_for_the_pi_and_is_kept_on_done(registry):
    """(18) Acceptance 5: matched 400 lx, black 0.001 in a session -> the picture document carries
    the light at a higher picture change; Done waits until a report answers it presented; Done
    stores it on the Display and the picture history's kept entry records it. Mutation probe: leave
    matched_lux out of picture_columns.picture_fields -> Done does not store it."""
    player = _bound(registry)
    before = _picture(registry)
    assert before.light == UPGRADE_DISPLAY_LIGHT
    with _client(registry) as client:
        session = _begin(client)
        assert _light(client, session, 400, 0.001, 1).json() == {"sequence": 2}
        document = _picture(registry)
        assert document.light == DisplayLight(matched_lux=400.0, black=0.001)
        assert document.picture_change > before.picture_change
        assert _view(client)["light"] == {"matched_lux": 400.0, "black": 0.001}
        early = _done(client, session)
        assert (early.status_code, early.json()) == (409, {"error": "not_presented_yet"})
        _picture_report(registry, for_change=document.picture_change, presented=document.picture_change)
        assert _done(client, session).status_code == 204
        display_id = _view(client)["display_id"]
        history = client.get(f"/v1/operator/displays/{display_id}/picture-history", headers=AUTH).json()
    row = _display(registry, player)
    assert (row["matched_lux"], row["panel_black"]) == (400.0, 0.001)
    kept = next(entry for entry in history["entries"] if entry["kind"] == "kept-done")
    assert kept["picture"]["light"] == {"matched_lux": 400.0, "black": 0.001}


def test_revert_puts_back_the_stored_light(registry):
    """(19) Revert puts the stored light back."""
    player = _bound(registry)
    _sql(registry, "UPDATE displays SET matched_lux=700, panel_black=0.002")
    with _client(registry) as client:
        session = _begin(client)
        assert _light(client, session, 300, 0.0005, 1).status_code == 200
        assert _picture(registry).light == DisplayLight(matched_lux=300.0, black=0.0005)
        _revert(client, session)
    assert _picture(registry).light == DisplayLight(matched_lux=700.0, black=0.002)
    assert (_display(registry, player)["matched_lux"], _display(registry, player)["panel_black"]) == (700.0, 0.002)


def test_done_never_counts_a_report_for_a_document_whose_light_differs():
    """(20) Pure: a report answering a document whose light differs from the draft is not
    `presented`, so Done waits. Mutation probe: `carries` leaves the light out -> presented."""
    draft = DisplayPicture({}, NEUTRAL_SOFTWARE, SignalRange.AUTO, DisplayLight(matched_lux=400.0, black=0.001))
    report = PictureReport("HDMI-A-1", NO_SERIAL, OFFERED, 3, {}, 3,
                           SignalReport(3, SignalRange.AUTO, SignalResult.APPLIED))
    assert not presented(draft, PictureDocument("HDMI-A-1", 3, {}, NEUTRAL_SOFTWARE, light=UPGRADE_DISPLAY_LIGHT),
                         report)
    assert presented(draft, PictureDocument("HDMI-A-1", 3, {}, NEUTRAL_SOFTWARE, light=draft.light), report)
