"""Central recognises a display from each Output's EDID (roadmap 1b, slice C1; owner q2, 2026-10-10).

Every report travels the real path: a `PgLinkStore` commit on the display line's state bucket, whose
`OutputReportJudge` records it on its Output in the same transaction. Bindings go through the
Registry. (1) A display with a usable serial is the same Display on another Pi, settings and all.
(2) A display with no usable serial is tied to its Frame: the same Display through a Pi swap, a new
one on another Frame. (3) The owner's case: two identical no-serial monitors on one Pi's two ports
are two Displays. (4) On an unbound Output it is pending until the Output is bound. (5) An unplug or
a blip keeps the Display and identity recorded. (6) Two Outputs reporting one serial at once make it
shared. (7) The Output document's change rises only when its body changes.
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from central.app import create_app
from central.content_catalog.catalog import device_id_for_serial
from central.infra.display_store import (
    DOCUMENT_STREAM,
    REPORT_STREAM,
    DisplayDocuments,
    DisplayWakes,
    OutputReportJudge,
)
from central.infra.node_link_store import PgLinkStores
from central.registry import Enrollment, FrameCreate, OutputReport, enrollment_message
from contracts.models import FrameProfile
from contracts.node_link import Pipe
from contracts.node_output import (
    BEST_DETECTED,
    DisplayIdentity,
    DisplayMode,
    PowerMethod,
    decode_output_document,
    encode_output_report,
    output_document_key,
    output_report_key,
)
from contracts.node_output import OutputReport as WireReport
from nodeapi.epoch import Token
from nodeapi.pull import Batch, Read

ADMIN = "test-operator-" + "x" * 40
AUTH = {"Authorization": "Bearer " + ADMIN}
EPOCH = "0123456789abcdef"
MODE = (DisplayMode(1920, 1080, 60000, preferred=True),)
SERIAL_DISPLAY = DisplayIdentity("DEL", 41200, "DELL U2720Q", "CN0F1X7K")
NO_SERIAL = DisplayIdentity("XYM", 5475, "MNN", None)          # the test Pi's portable monitor (spike)
_SEQ = itertools.count(1)


def enroll_pi(registry, serial: str) -> str:
    """A Pi with two HDMI ports, enrolled under its bus serial's device id; its player id."""
    device_id = device_id_for_serial(serial)
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO devices(device_id, serial, first_seen, last_seen) VALUES (%s, %s, 1, 1)",
                     (device_id, serial))
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw().hex()
    boot_id = str(uuid.uuid4())
    nonce = registry.challenge(public)["nonce"]
    outputs = tuple(OutputReport(output_id=f"HDMI-A-{i + 1}", width_px=1920, height_px=1080) for i in range(2))
    request = Enrollment(public_key=public, nonce=nonce, outputs=outputs, device_id=device_id, boot_id=boot_id,
                         ticket_id=None, signature=base64.b64encode(key.sign(enrollment_message(
                             nonce, outputs, device_id, boot_id, None))).decode())
    return registry.enroll(request)["player_id"]


def _frame(registry, frame_id: str) -> str:
    registry.create_frame(FrameCreate(id=frame_id, width_mm=300, height_mm=500,
                                      profile=FrameProfile(width_px=1080, height_px=1920, diagonal_inches=24)))
    return frame_id


def _generation(registry, frame_id: str) -> int:
    with registry.db.transaction() as conn:
        return conn.execute("SELECT generation FROM frames WHERE id=%s", (frame_id,)).fetchone()["generation"]


def _bind(registry, frame_id: str, player_id: str, output_id: str) -> None:
    registry.bind(frame_id, player_id, output_id, expected_generation=_generation(registry, frame_id))


def _unbind(registry, frame_id: str) -> None:
    registry.unbind(frame_id, expected_generation=_generation(registry, frame_id))


def _report(registry, serial: str, output_id: str, identity: DisplayIdentity | None, *, connected: bool = True,
            data: bytes | None = None) -> None:
    """The Pi's Output report, recorded by Central's link store on the display line's state bucket."""
    report = WireReport(output_id, connected, identity, MODE if identity else (), (), None, None, None, None)
    token = Token(EPOCH, next(_SEQ))
    read = Read(token, "$KV.state_display." + output_report_key(output_id), {},
                encode_output_report(report) if data is None else data)
    stores = PgLinkStores(registry.db, {REPORT_STREAM: OutputReportJudge(DisplayWakes())})
    asyncio.run(stores.store(serial, Pipe.FLEET).commit(REPORT_STREAM, Batch((read,), token, False)))


def _seen(registry, player_id: str, output_id: str) -> dict:
    """The Output's row joined to the Display recorded there."""
    with registry.db.transaction() as conn:
        return conn.execute(
            "SELECT o.connected, o.identity, o.display_id, d.serial, d.frame_id, d.power_method, "
            "d.switch_input_on_power_on, d.never_off_on_other_input, d.modes FROM output_displays o "
            "LEFT JOIN displays d ON d.id=o.display_id WHERE o.player_id=%s AND o.output_id=%s",
            (player_id, output_id)).fetchone()


def _sql(registry, sql: str, params=()) -> list[dict]:
    with registry.db.transaction() as conn:
        cursor = conn.execute(sql, params)
        return cursor.fetchall() if cursor.description else []


def _documents(registry, serial: str, pipe: Pipe = Pipe.FLEET) -> dict:
    stores = PgLinkStores(registry.db)
    source = DisplayDocuments(stores, DisplayWakes()).source(serial, pipe)
    return asyncio.run(source.documents(DOCUMENT_STREAM))


def test_a_display_with_a_usable_serial_keeps_its_settings_on_another_pi(registry):
    pi_a, pi_b = enroll_pi(registry, "pi-a"), enroll_pi(registry, "pi-b")
    _report(registry, "pi-a", "HDMI-A-1", SERIAL_DISPLAY)
    first = _seen(registry, pi_a, "HDMI-A-1")
    assert first["display_id"] is not None and first["serial"] == "CN0F1X7K" and first["frame_id"] is None
    _sql(registry, "UPDATE displays SET power_method='ddc-ci', switch_input_on_power_on=false WHERE id=%s",
         (first["display_id"],))

    _report(registry, "pi-a", "HDMI-A-1", None, connected=False)   # unplugged from Pi A
    _report(registry, "pi-b", "HDMI-A-2", SERIAL_DISPLAY)           # and plugged into Pi B

    moved = _seen(registry, pi_b, "HDMI-A-2")
    assert moved["display_id"] == first["display_id"]
    assert (moved["power_method"], moved["switch_input_on_power_on"]) == ("ddc-ci", False)
    document = decode_output_document(_documents(registry, "pi-b")[output_document_key("HDMI-A-2")])
    assert (document.method, document.switch_input_on_power_on) == (PowerMethod.DDC_CI, False)
    assert _sql(registry, "SELECT count(*) AS n FROM displays")[0]["n"] == 1


def test_a_display_without_a_serial_is_tied_to_the_frame_it_feeds(registry):
    pi_a, pi_b = enroll_pi(registry, "pi-a"), enroll_pi(registry, "pi-b")
    frame, other = _frame(registry, "kitchen"), _frame(registry, "hall")
    _bind(registry, frame, pi_a, "HDMI-A-1")
    _report(registry, "pi-a", "HDMI-A-1", NO_SERIAL)
    first = _seen(registry, pi_a, "HDMI-A-1")
    assert first["display_id"] is not None and (first["serial"], first["frame_id"]) == (None, frame)
    _sql(registry, "UPDATE displays SET power_method='hdmi-cec' WHERE id=%s", (first["display_id"],))

    # A Pi swap: the Frame is rebound to Pi B's port, the same display on it.
    _unbind(registry, frame)
    _bind(registry, frame, pi_b, "HDMI-A-1")
    _report(registry, "pi-b", "HDMI-A-1", NO_SERIAL)
    assert _seen(registry, pi_b, "HDMI-A-1")["display_id"] == first["display_id"]

    # The display moved to another Frame starts over: a new Display with default settings.
    _bind(registry, other, pi_b, "HDMI-A-2")
    _report(registry, "pi-b", "HDMI-A-2", NO_SERIAL)
    moved = _seen(registry, pi_b, "HDMI-A-2")
    assert moved["display_id"] not in (None, first["display_id"]) and moved["frame_id"] == other
    assert (moved["power_method"], moved["switch_input_on_power_on"], moved["never_off_on_other_input"]) == (
        None, True, True)

    # So does the display left on Pi A's port when another Frame is bound there: no report needed.
    third = _frame(registry, "landing")
    _bind(registry, third, pi_a, "HDMI-A-1")
    rebound = _seen(registry, pi_a, "HDMI-A-1")
    assert rebound["display_id"] not in (first["display_id"], moved["display_id"]) and rebound["frame_id"] == third


def test_two_identical_monitors_without_a_serial_on_one_pi_are_two_displays(registry):
    """Owner, 2026-10-10: "There might be duplicate make/model attached to both pi HDMI's"."""
    pi = enroll_pi(registry, "pi-a")
    left, right = _frame(registry, "left"), _frame(registry, "right")
    _bind(registry, left, pi, "HDMI-A-1")
    _bind(registry, right, pi, "HDMI-A-2")
    _report(registry, "pi-a", "HDMI-A-1", NO_SERIAL)
    _report(registry, "pi-a", "HDMI-A-2", NO_SERIAL)
    one, two = _seen(registry, pi, "HDMI-A-1"), _seen(registry, pi, "HDMI-A-2")
    assert None not in (one["display_id"], two["display_id"]) and one["display_id"] != two["display_id"]
    assert (one["frame_id"], two["frame_id"]) == (left, right)


def test_a_display_without_a_serial_on_an_unbound_output_is_pending_until_bound(registry):
    pi = enroll_pi(registry, "pi-a")
    frame = _frame(registry, "kitchen")
    _report(registry, "pi-a", "HDMI-A-1", NO_SERIAL)
    pending = _seen(registry, pi, "HDMI-A-1")
    assert pending["display_id"] is None and pending["identity"]["maker"] == "XYM"
    assert _sql(registry, "SELECT count(*) AS n FROM displays")[0]["n"] == 0

    _bind(registry, frame, pi, "HDMI-A-1")
    resolved = _seen(registry, pi, "HDMI-A-1")
    assert resolved["display_id"] is not None and resolved["frame_id"] == frame
    assert resolved["modes"] == [{"width": 1920, "height": 1080, "refresh_millihertz": 60000, "preferred": True}]


def test_an_unplug_or_a_blip_keeps_the_display_recorded(registry):
    pi = enroll_pi(registry, "pi-a")
    _bind(registry, _frame(registry, "kitchen"), pi, "HDMI-A-1")
    _report(registry, "pi-a", "HDMI-A-1", NO_SERIAL)
    before = _seen(registry, pi, "HDMI-A-1")
    _report(registry, "pi-a", "HDMI-A-1", None, connected=False)   # unplug, or the HDMI signal blips
    after = _seen(registry, pi, "HDMI-A-1")
    assert after["connected"] is False
    assert (after["display_id"], after["identity"]) == (before["display_id"], before["identity"])
    _report(registry, "pi-a", "HDMI-A-1", None)                      # connected, EDID unreadable now
    assert _seen(registry, pi, "HDMI-A-1")["display_id"] == before["display_id"]


def test_one_serial_reported_by_two_outputs_at_once_is_shared(registry):
    pi = enroll_pi(registry, "pi-a")
    left, right = _frame(registry, "left"), _frame(registry, "right")
    _bind(registry, left, pi, "HDMI-A-1")
    _bind(registry, right, pi, "HDMI-A-2")
    _report(registry, "pi-a", "HDMI-A-1", SERIAL_DISPLAY)
    _report(registry, "pi-a", "HDMI-A-2", SERIAL_DISPLAY)
    assert _sql(registry, "SELECT maker, product, serial FROM shared_serials") == [
        {"maker": "DEL", "product": 41200, "serial": "CN0F1X7K"}]
    second = _seen(registry, pi, "HDMI-A-2")
    assert (second["serial"], second["frame_id"]) == (None, right)
    assert second["display_id"] != _seen(registry, pi, "HDMI-A-1")["display_id"]


def test_the_output_document_changes_only_when_its_body_does(registry):
    pi = enroll_pi(registry, "pi-a")
    _bind(registry, _frame(registry, "kitchen"), pi, "HDMI-A-1")
    first = _documents(registry, "pi-a")
    assert sorted(first) == [output_document_key("HDMI-A-1"), output_document_key("HDMI-A-2")]
    document = decode_output_document(first[output_document_key("HDMI-A-1")])
    assert (document.change, document.method, [request.request_id for request in document.power]) == (
        1, BEST_DETECTED, ["standing"])
    assert _documents(registry, "pi-a") == first                     # unchanged: the same bytes, change 1

    _report(registry, "pi-a", "HDMI-A-1", NO_SERIAL)
    assert _documents(registry, "pi-a") == first                     # a Display with default settings
    _sql(registry, "UPDATE displays SET power_method='signal-off'")
    changed = decode_output_document(_documents(registry, "pi-a")[output_document_key("HDMI-A-1")])
    assert (changed.change, changed.method) == (2, PowerMethod.SIGNAL_OFF)
    again = decode_output_document(_documents(registry, "pi-a")[output_document_key("HDMI-A-1")])
    assert again.change == 2
    assert decode_output_document(_documents(registry, "pi-a")[output_document_key("HDMI-A-2")]).change == 1
    assert _documents(registry, "pi-a", Pipe.SHOW) == {}


def test_an_undecodable_report_is_skipped_and_the_rest_recorded(registry):
    pi = enroll_pi(registry, "pi-a")
    _report(registry, "pi-a", "HDMI-A-1", None, data=b'{"not": "a report"}')
    assert _seen(registry, pi, "HDMI-A-1") is None
    assert len(_sql(registry, "SELECT 1 FROM node_link_records WHERE stream=%s", (REPORT_STREAM,))) == 1


def test_the_hardware_tab_reads_the_display_on_a_frames_output(registry):
    pi = enroll_pi(registry, "pi-a")
    frame = _frame(registry, "kitchen")
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        assert client.get("/v1/operator/frames/nowhere/display", headers=AUTH).json() == {"error": "unknown_frame"}
        unbound = client.get(f"/v1/operator/frames/{frame}/display", headers=AUTH).json()
        assert (unbound["readiness"], unbound["display"], unbound["connected"]) == ("unbound", None, None)
        _bind(registry, frame, pi, "HDMI-A-1")
        _report(registry, "pi-a", "HDMI-A-1", NO_SERIAL)
        response = client.get(f"/v1/operator/frames/{frame}/display", headers=AUTH)
        assert response.status_code == 200
        body = response.json()
        assert (body["player_id"], body["output_id"], body["connected"]) == (pi, "HDMI-A-1", True)
        assert {key: body["display"][key] for key in ("maker", "product", "name", "serial", "tied_to_frame")} == {
            "maker": "XYM", "product": 5475, "name": "MNN", "serial": None, "tied_to_frame": True}
        assert client.get(f"/v1/operator/frames/{frame}/display").status_code == 401
