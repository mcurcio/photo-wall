"""Picture settings on the Display (roadmap 1c, slices C1 and C2; design rules 1 and 3; owner q1, q3).

Every report travels the real path (`test_display_identity._report`: the link store's commit and its
Output report judge); every console write goes through the operator API the Picture tab calls; the
document is the one the worker's source projects (`DisplayDocuments`).

C1: (1) a Display's settings follow it to another Pi's Output; (2) the Display's first probe places
brightness and contrast, and a later probe never moves them; (3) the picture part has its own change
number; (4) an unplaced setting goes in the document unset; (5) and (6) migration 075 on Frames as
1b stored them; (7) the V1 calibration route is gone.
C2: (8) a stale sequence changes nothing; (9) Done waits for the Pi; (10) Revert; (11) an abandoned
session reverts with no request; (12) Copy; (13) the picture is never a Position commit; (14) a
setting is placed on the display only when the display answered.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from support.position import save_position
from test_coordination import schedule, setup_players
from test_display_identity import (
    ADMIN,
    AUTH,
    MODE,
    NO_SERIAL,
    SERIAL_DISPLAY,
    _bind,
    _frame,
    _report,
    _seen,
    _sql,
    enroll_pi,
)

from central.app import create_app
from central.coordination import Coordinator
from central.displays.model import Readiness
from central.infra.display_store import DOCUMENT_STREAM, DisplayDocuments, DisplayWakes
from central.infra.node_link_store import PgLinkStores
from contracts.models import Calibration, CoverFit, Plan
from contracts.node_link import Pipe
from contracts.node_output import (
    AS_THE_DISPLAY_HAS_IT,
    ActsOn,
    DisplayIdentity,
    OutputDocument,
    PictureReport,
    PictureSetting,
    SettingReport,
    WriteResult,
    decode_output_document,
    encode_output_report,
    output_document_key,
)
from contracts.node_output import OutputReport as WireReport

MIGRATION = Path(__file__).parents[1] / "central/migrations/075_display_picture.sql"
SERIAL = "pi-picture"
UNSET_ADJUSTMENT = PictureSetting(ActsOn.PICTURE_ADJUSTMENT, AS_THE_DISPLAY_HAS_IT)
SKEWED_CORNERS = [[0.1, 0.05], [0.95, 0.0], [1.0, 1.0], [0.0, 0.9]]


def _projected(registry, serial: str = SERIAL, output_id: str = "HDMI-A-1") -> OutputDocument:
    """The Output's document as the worker's source projects (and records) it now."""
    source = DisplayDocuments(PgLinkStores(registry.db), DisplayWakes()).source(serial, Pipe.FLEET)
    return decode_output_document(asyncio.run(source.documents(DOCUMENT_STREAM))[output_document_key(output_id)])


def _probe(registry, *, brightness: bool, contrast: bool, for_change: int = 1, read: int | None = None,
           presented: int | None = None, result: WriteResult | None = None, serial: str = SERIAL,
           output_id: str = "HDMI-A-1", identity: DisplayIdentity = NO_SERIAL) -> None:
    """The Pi's report with a picture part: which settings the display answered, and for
    `for_change` brightness on the display (read back `read`) and contrast as picture adjustment."""
    picture = PictureReport(brightness, contrast, for_change,
                            SettingReport(ActsOn.DISPLAY, read, result) if brightness
                            else SettingReport(ActsOn.PICTURE_ADJUSTMENT, None, None),
                            SettingReport(ActsOn.DISPLAY, read, None) if contrast
                            else SettingReport(ActsOn.PICTURE_ADJUSTMENT, None, None), presented)
    report = WireReport(output_id, True, identity, MODE, (), None, None, None, None, picture)
    _report(registry, serial, output_id, identity, data=encode_output_report(report))


def _display(registry, player_id: str, output_id: str = "HDMI-A-1") -> dict:
    [row] = _sql(registry, "SELECT d.* FROM output_displays o JOIN displays d ON d.id=o.display_id "
                           "WHERE o.player_id=%s AND o.output_id=%s", (player_id, output_id))
    return row


def _bound(registry, frame_id: str = "kitchen", serial: str = SERIAL) -> str:
    player = enroll_pi(registry, serial)
    _bind(registry, _frame(registry, frame_id), player, "HDMI-A-1")
    return player


def _client(registry) -> TestClient:
    return TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False))


def _begin(client, frame_id: str = "kitchen") -> dict:
    response = client.post(f"/v1/operator/frames/{frame_id}/picture-sessions", headers=AUTH)
    assert response.status_code == 201, response.text
    return response.json()


def _edit(client, session: dict, setting: str, value: int, expected: int):
    return client.post(f"/v1/operator/picture-sessions/{session['session_id']}/edits", headers=AUTH,
                       json={"setting": setting, "value": value, "expected_sequence": expected})


# --- C1 ------------------------------------------------------------------------------------------

def test_a_displays_settings_follow_it_to_another_pis_output(registry):
    """(1) Serial-keyed: the settings are the Display's, not the Output's."""
    pi_a, pi_b = enroll_pi(registry, "pi-a"), enroll_pi(registry, "pi-b")
    _probe(registry, brightness=True, contrast=False, serial="pi-a", identity=SERIAL_DISPLAY)
    display_id = _seen(registry, pi_a, "HDMI-A-1")["display_id"]
    _sql(registry, "UPDATE displays SET brightness=60, gamma_tenths=24 WHERE id=%s", (display_id,))
    _report(registry, "pi-a", "HDMI-A-1", None, connected=False)
    _probe(registry, brightness=False, contrast=False, serial="pi-b", output_id="HDMI-A-2",
           identity=SERIAL_DISPLAY)

    assert _seen(registry, pi_b, "HDMI-A-2")["display_id"] == display_id
    moved = _projected(registry, "pi-b", "HDMI-A-2").picture
    assert moved.brightness == PictureSetting(ActsOn.DISPLAY, 60)   # placed by its first probe, kept
    assert (moved.contrast, moved.gamma_tenths) == (UNSET_ADJUSTMENT, 24)


def test_the_first_probe_places_each_setting_and_a_later_probe_never_moves_it(registry):
    """(2) Mutation probe: re-place on every report -> the later probe moves contrast onto the display."""
    player = _bound(registry)
    _probe(registry, brightness=True, contrast=False)
    placed = _display(registry, player)
    assert (placed["brightness_acts_on"], placed["contrast_acts_on"]) == ("display", "picture-adjustment")
    assert (placed["brightness"], placed["contrast"]) == (None, None)    # nothing set: nothing written

    _probe(registry, brightness=True, contrast=True)
    kept = _display(registry, player)
    assert (kept["brightness_acts_on"], kept["contrast_acts_on"]) == ("display", "picture-adjustment")


def test_the_picture_part_has_its_own_change_number(registry):
    """(3) Mutation probe: raise `change` with the picture -> the edit raises it."""
    _bound(registry)
    _probe(registry, brightness=True, contrast=False)
    with _client(registry) as client:
        before = _projected(registry)
        session = _begin(client)
        assert _edit(client, session, "brightness", 70, 1).status_code == 200
        edited = _projected(registry)
        assert (edited.change, edited.picture_change) == (before.change, before.picture_change + 1)
        assert edited.picture.brightness == PictureSetting(ActsOn.DISPLAY, 70)

        assert client.post("/v1/operator/frames/kitchen/power-tests", headers=AUTH).status_code == 202
        tested = _projected(registry)
        assert (tested.change, tested.picture_change) == (edited.change + 1, edited.picture_change)
        assert _projected(registry) == tested   # unchanged facts: neither number moves


def test_an_unplaced_setting_goes_in_the_document_unset(registry):
    """(4) A Display seen with no picture part yet, and an Output with no Display at all."""
    player = _bound(registry)
    assert _projected(registry).picture.brightness == UNSET_ADJUSTMENT      # no Display yet
    _report(registry, SERIAL, "HDMI-A-1", NO_SERIAL)
    assert _display(registry, player)["brightness_acts_on"] is None
    _sql(registry, "UPDATE displays SET brightness=55")                       # set, but not placed
    document = _projected(registry)
    assert (document.picture.brightness, document.picture.contrast) == (UNSET_ADJUSTMENT, UNSET_ADJUSTMENT)
    assert (document.picture.colour_temperature_kelvin, document.picture.gamma_tenths) == (6500, 22)


def _before_075(registry) -> None:
    """The database as 1b's build left it: no picture columns, the preview columns back."""
    _sql(registry, "DROP TABLE picture_sessions")
    _sql(registry, "ALTER TABLE displays DROP COLUMN brightness_acts_on, DROP COLUMN brightness, "
                   "DROP COLUMN contrast_acts_on, DROP COLUMN contrast, DROP COLUMN colour_temperature_kelvin, "
                   "DROP COLUMN gamma_tenths")
    _sql(registry, "ALTER TABLE output_documents DROP COLUMN picture_change, DROP COLUMN picture_digest")
    _sql(registry, "ALTER TABLE frames ADD COLUMN preview JSONB, ADD COLUMN preview_expires DOUBLE PRECISION")


def _migrate(registry) -> None:
    with registry.db.transaction() as conn:
        conn.execute(MIGRATION.read_text())


def _stored_1b(registry, frame_id: str, *, gain: float, rotation: int = 0, corners=None) -> None:
    calibration = {"revision": 2, "rotation": rotation, "corners": corners or [[0, 0], [1, 0], [1, 1], [0, 1]],
                   "crop": [0.1, 0, 1, 1], "gain": gain}
    _sql(registry, "UPDATE frames SET calibration=%s, position_generation=generation WHERE id=%s",
         (Jsonb(calibration), frame_id))


def _frame_row(registry, frame_id: str):
    return next(frame for frame in registry.inventory().frames if frame.id == frame_id)


def test_075_moves_a_frames_gain_to_its_displays_brightness(registry):
    """(5) Mutation probe: write gain 1.0 as 50 % -> the second Display is set."""
    player = enroll_pi(registry, SERIAL)
    for frame_id, output_id in (("bright", "HDMI-A-1"), ("plain", "HDMI-A-2")):
        _bind(registry, _frame(registry, frame_id), player, output_id)
        _report(registry, SERIAL, output_id, NO_SERIAL)
    _before_075(registry)
    _stored_1b(registry, "bright", gain=1.5)
    _stored_1b(registry, "plain", gain=1.0)
    _migrate(registry)

    bright, plain = _display(registry, player, "HDMI-A-1"), _display(registry, player, "HDMI-A-2")
    assert (bright["brightness_acts_on"], bright["brightness"]) == ("picture-adjustment", 75)
    assert (plain["brightness_acts_on"], plain["brightness"]) == (None, None)
    assert (bright["contrast_acts_on"], bright["colour_temperature_kelvin"], bright["gamma_tenths"]) == (
        None, 6500, 22)


def test_075_moves_every_frame_to_cover_at_angle_0_and_not_ready(registry):
    """(6) Owner q1 and his rotation answer (2026-10-10, "Every frame should reset to 0 rotation"):
    a Frame stored with corners C and rotation 90 reads Cover with C as its opening at angle 0; its
    revision and configuration revision rise; trim and gain are gone; it is not ready until its
    Position is saved again. Mutation probe: drop the readiness statement -> the Frame stays ready."""
    player = _bound(registry)
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
    saved = Calibration.model_validate(row["calibration"])
    assert saved.fit == CoverFit(corners=tuple(map(tuple, SKEWED_CORNERS)), rotation=0)
    assert _frame_row(registry, "kitchen").readiness is Readiness.POSITION_NEEDED
    assert not _sql(registry, "SELECT 1 FROM information_schema.columns WHERE table_name='frames' "
                              "AND column_name IN ('preview', 'preview_expires')")
    epoch = next(p.authority_epoch for p in registry.inventory().players if p.id == player)
    assert registry.configuration_for(player, epoch)["execution_bindings"] == []


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
    """(6b) A Player's last configuration and its plan offers hold copies of the Frame's Position;
    after 075 the strict 1c models still read them, so Central hands the Player a configuration and
    plans. Mutation probe: drop the two binding rewrites -> `configuration` raises."""
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

    configuration = coordinator.configuration(player["player_id"], epoch)
    [binding] = configuration.bindings
    assert binding.calibration.fit == CoverFit(corners=tuple(map(tuple, SKEWED_CORNERS)), rotation=0)
    coordinator.advance()
    for row in _sql(registry, "SELECT manifest FROM plan_offers"):
        Plan.model_validate(row["manifest"])


def test_075_gives_a_stored_1b_report_and_document_a_picture_part(registry):
    """(6c) A Pi's report and Central's document as 1b stored them (no picture part) still decode
    after 075, so the Power and Picture tabs answer before the Pi reports again. Mutation probe:
    drop the report statement -> the Power tab answers 422."""
    _bound(registry)
    _probe(registry, brightness=True, contrast=False)
    _projected(registry)
    _before_075(registry)
    _sql(registry, "UPDATE output_displays SET report = report - 'picture'")
    _sql(registry, "UPDATE output_documents SET document = document - 'picture' - 'picture_change'")
    _migrate(registry)

    with _client(registry) as client:
        assert client.get("/v1/operator/frames/kitchen/power", headers=AUTH).status_code == 200
        assert client.get("/v1/operator/frames/kitchen/picture", headers=AUTH).status_code == 200
    projected = _projected(registry)
    assert projected.picture_change == 2     # the neutral part's change 1, then the Display's own


def test_the_v1_calibration_route_is_gone(registry):
    """(7) Position is committed only by its trial's Save."""
    _bound(registry)
    with _client(registry) as client:
        answer = client.post("/v1/operator/frames/kitchen/calibration", headers=AUTH, json={
            "operation": "commit", "expected_revision": 1, "expected_generation": 1, "calibration": {}})
    assert answer.status_code in (404, 405)


# --- C2 ------------------------------------------------------------------------------------------

def test_an_edit_against_a_stale_sequence_changes_nothing(registry):
    """(8) Mutation probe: overwrite the whole record on edit -> the stale edit lands."""
    _bound(registry)
    _probe(registry, brightness=True, contrast=False)
    with _client(registry) as client:
        session = _begin(client)
        assert _edit(client, session, "brightness", 70, 1).json() == {"sequence": 2}
        stale = _edit(client, session, "contrast", 30, 1)
        assert (stale.status_code, stale.json()) == (409, {"error": "stale_sequence"})
        picture = client.get("/v1/operator/frames/kitchen/picture", headers=AUTH).json()
        assert (picture["brightness"]["value"], picture["contrast"]["value"]) == (70, None)
        assert picture["session"]["sequence"] == 2
        refused = _edit(client, session, "gamma", 27, 2)
        assert (refused.status_code, refused.json()) == (422, {"error": "out_of_range"})


def test_done_waits_until_the_pi_answers_the_latest_picture_change(registry):
    """(9) Mutation probe: Done without the report check -> the first Done is taken."""
    player = _bound(registry)
    _probe(registry, brightness=True, contrast=False, read=40)
    with _client(registry) as client:
        session = _begin(client)
        _edit(client, session, "brightness", 70, 1)
        latest = _projected(registry)
        done = f"/v1/operator/picture-sessions/{session['session_id']}/done"
        early = client.post(done, headers=AUTH)
        assert (early.status_code, early.json()) == (409, {"error": "not_presented_yet"})
        assert client.get("/v1/operator/frames/kitchen/picture", headers=AUTH).json()["status"] == "waiting"

        _probe(registry, brightness=True, contrast=False, for_change=latest.picture_change, read=70,
               result=WriteResult.TAKEN, presented=latest.picture_change)
        picture = client.get("/v1/operator/frames/kitchen/picture", headers=AUTH).json()
        assert picture["status"] == "shown"
        assert (picture["brightness"]["read"], picture["brightness"]["result"]) == (70, "taken")
        assert client.post(done, headers=AUTH).status_code == 204
    stored = _display(registry, player)
    assert (stored["brightness_acts_on"], stored["brightness"]) == ("display", 70)
    assert _sql(registry, "SELECT id FROM picture_sessions") == []


def test_revert_puts_back_the_stored_values_and_an_unset_one_as_the_display_read_it(registry):
    """(10) Brightness was unset (as the display has it) and edited: Revert sets it to the 40 % the
    display read before the session, so the display shows what it did. Contrast was set: back to it."""
    player = _bound(registry)
    _probe(registry, brightness=True, contrast=True, read=40)
    _sql(registry, "UPDATE displays SET contrast=55")
    with _client(registry) as client:
        session = _begin(client)
        _edit(client, session, "brightness", 90, 1)
        _edit(client, session, "contrast", 10, 2)
        _edit(client, session, "colour-temperature", 3200, 3)
        assert client.delete(f"/v1/operator/picture-sessions/{session['session_id']}",
                             headers=AUTH).status_code == 204
        gone = _edit(client, session, "brightness", 20, 4)
        assert (gone.status_code, gone.json()) == (404, {"error": "unknown_session"})
    stored = _display(registry, player)
    assert (stored["brightness"], stored["contrast"], stored["colour_temperature_kelvin"]) == (40, 55, 6500)
    assert _projected(registry).picture.brightness == PictureSetting(ActsOn.DISPLAY, 40)


def test_an_abandoned_session_reverts_with_no_request(registry):
    """(11) Past its idle deadline the document shows the reverted values with no request made; the
    next session stores them first. Mutation probe: a projection that ignores `idle_until` -> the
    draft stays in the document."""
    player = _bound(registry)
    _probe(registry, brightness=True, contrast=False, read=40)
    with _client(registry) as client:
        session = _begin(client)
        _edit(client, session, "brightness", 90, 1)
        assert _projected(registry).picture.brightness == PictureSetting(ActsOn.DISPLAY, 90)
        _sql(registry, "UPDATE picture_sessions SET idle_until = EXTRACT(EPOCH FROM clock_timestamp()) - 1")
        assert _projected(registry).picture.brightness == PictureSetting(ActsOn.DISPLAY, 40)
        ended = client.post(f"/v1/operator/picture-sessions/{session['session_id']}/keepalive", headers=AUTH)
        assert (ended.status_code, ended.json()) == (410, {"error": "session_ended"})
        assert _display(registry, player)["brightness"] is None    # no request has stored anything yet
        _begin(client)
    assert _display(registry, player)["brightness"] == 40


def test_the_sources_wait_ends_at_a_sessions_idle_deadline(registry):
    """(11, the wake) The source's changed() returns at the session's idle deadline, with no wake."""
    _bound(registry)
    _probe(registry, brightness=True, contrast=False)
    with _client(registry) as client:
        _begin(client)
    _sql(registry, "UPDATE picture_sessions SET idle_until = EXTRACT(EPOCH FROM clock_timestamp()) + 1")
    source = DisplayDocuments(PgLinkStores(registry.db), DisplayWakes()).source(SERIAL, Pipe.FLEET)

    async def ended() -> None:
        await asyncio.wait_for(source.changed(), 5)
    asyncio.run(ended())


def test_copy_carries_the_four_values_never_where_they_act_nor_position(registry):
    """(12) Mutation probe: Copy carrying `acts_on` -> B's contrast moves onto the display."""
    pi_a, pi_b = enroll_pi(registry, "pi-a"), enroll_pi(registry, "pi-b")
    _bind(registry, _frame(registry, "a"), pi_a, "HDMI-A-1")
    _bind(registry, _frame(registry, "b"), pi_b, "HDMI-A-1")
    _bind(registry, _frame(registry, "c"), pi_b, "HDMI-A-2")
    _frame(registry, "d")                                                  # never bound: no Display
    _probe(registry, brightness=True, contrast=True, serial="pi-a", identity=SERIAL_DISPLAY)
    _probe(registry, brightness=False, contrast=False, serial="pi-b")
    save_position(registry, "b")
    _sql(registry, "UPDATE displays SET brightness=65, contrast=35, colour_temperature_kelvin=5000, gamma_tenths=20 "
                   "WHERE serial IS NOT NULL")
    # A's display moves to C's port: C is fed by A's own Display.
    _report(registry, "pi-a", "HDMI-A-1", None, connected=False)
    _report(registry, "pi-b", "HDMI-A-2", SERIAL_DISPLAY)
    before = _sql(registry, "SELECT calibration, profile, position_generation FROM frames WHERE id='b'")
    with _client(registry) as client:
        copied = client.post("/v1/operator/frames/a/picture-copies", headers=AUTH, json={"to_frames": ["b", "c", "d"]})
        assert copied.status_code == 200, copied.text
        assert copied.json() == [{"frame_id": "b", "outcome": "copied"}, {"frame_id": "c", "outcome": "same-display"},
                                 {"frame_id": "d", "outcome": "no-display"}]
        unknown = client.post("/v1/operator/frames/a/picture-copies", headers=AUTH, json={"to_frames": ["zz"]})
        assert (unknown.status_code, unknown.json()) == (404, {"error": "unknown_frame"})
    target = _display(registry, pi_b, "HDMI-A-1")
    assert (target["brightness"], target["contrast"], target["colour_temperature_kelvin"], target["gamma_tenths"]) == (
        65, 35, 5000, 20)
    assert (target["brightness_acts_on"], target["contrast_acts_on"]) == ("picture-adjustment", "picture-adjustment")
    assert _sql(registry, "SELECT calibration, profile, position_generation FROM frames WHERE id='b'") == before


def test_a_picture_edit_and_done_are_never_a_position_commit(registry):
    """(13) Mutation probe: an edit raising the position revision -> the revisions move."""
    player = _bound(registry)
    _probe(registry, brightness=True, contrast=False)
    save_position(registry, "kitchen")
    columns = "SELECT calibration, configuration_revision, generation, position_generation FROM frames"
    before = _sql(registry, columns)
    with _client(registry) as client:
        session = _begin(client)
        _edit(client, session, "brightness", 30, 1)
        latest = _projected(registry)
        _probe(registry, brightness=True, contrast=False, for_change=latest.picture_change, read=30,
               presented=latest.picture_change, result=WriteResult.TAKEN)
        assert client.post(f"/v1/operator/picture-sessions/{session['session_id']}/done",
                           headers=AUTH).status_code == 204
    assert _sql(registry, columns) == before
    assert _frame_row(registry, "kitchen").readiness is Readiness.READY
    assert _display(registry, player)["brightness"] == 30


def test_a_setting_goes_onto_the_display_only_when_the_display_answered(registry):
    """(14) Rule 1: a setting acts on the display only where the display took a DDC/CI read."""
    _bound(registry)
    _probe(registry, brightness=True, contrast=False)
    with _client(registry) as client:
        session = _begin(client)
        place = f"/v1/operator/picture-sessions/{session['session_id']}/places"
        refused = client.post(place, headers=AUTH,
                              json={"setting": "contrast", "acts_on": "display", "expected_sequence": 1})
        assert (refused.status_code, refused.json()) == (409, {"error": "display_did_not_answer"})
        moved = client.post(place, headers=AUTH,
                            json={"setting": "brightness", "acts_on": "picture-adjustment", "expected_sequence": 1})
        assert moved.json() == {"sequence": 2}
        assert _projected(registry).picture.brightness == UNSET_ADJUSTMENT
        assert client.post("/v1/operator/frames/kitchen/picture-sessions", headers=AUTH).json() == {
            "error": "session_open"}


def test_a_session_needs_a_bound_frame_with_a_display(registry):
    enroll_pi(registry, SERIAL)
    _frame(registry, "loose")
    _bound(registry, "kitchen", "pi-other")
    with _client(registry) as client:
        assert client.post("/v1/operator/frames/nowhere/picture-sessions", headers=AUTH).json() == {
            "error": "unknown_frame"}
        assert client.post("/v1/operator/frames/loose/picture-sessions", headers=AUTH).json() == {
            "error": "frame_unbound"}
        assert client.post("/v1/operator/frames/kitchen/picture-sessions", headers=AUTH).json() == {
            "error": "no_display"}
        picture = client.get("/v1/operator/frames/loose/picture", headers=AUTH).json()
        assert (picture["status"], picture["display_id"], picture["session"]) == ("no-display", None, None)
        assert client.post("/v1/operator/frames/kitchen/picture-sessions").status_code == 401
