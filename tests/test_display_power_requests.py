"""Test off and on as a timed power request in the Output document (roadmap 1b, slice C3; owner q1,
2026-10-10: "5 minutes, or less if Turn on is pressed").

Every request goes through the operator API that the Power tab calls; every Output report travels
the link store's commit and its judge (test_display_identity's helpers); the projection is the
document source the worker's NodeLinks read. (1) A test off on a bound Frame answers 202 and the
projection carries it on top of standing on. (2) Turn on ends it: the request underneath takes over,
and the tab reads the Pi's answer for that request. (3) An unbound Frame
is refused. (4) Past `ends_at` the projection drops the test and the change rises; the source's
`changed()` returns at that end. (5) The Power tab's status waits until a report names the change
carrying the test, then answers with its result; a report naming an older change keeps waiting.
(6) Each command wakes the worker through PostgreSQL's notification. (7) The power settings change
the document. Roadmap 2a (Power and the week, A9): the test sits on top of the Frame's power
timetable; the document's `change` rises at a set time and at house midnight, never between, and the
Power tab reads what the Pi carries out through InForceView.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from support.house_time import LONDON, set_house
from test_display_identity import ADMIN, AUTH, NO_SERIAL, _bind, _frame, _report, enroll_pi

from central.app import create_app
from central.content_catalog.catalog import device_id_for_serial
from central.db import DatabaseTransactionClock
from central.displays.model import TEST_SECONDS
from central.infra.display_store import (
    DOCUMENT_STREAM,
    DisplayDocuments,
    DisplayWakes,
    _until_next_deadline,
)
from central.infra.node_link_store import PgLinkStores
from contracts.node_link import Pipe
from contracts.node_output import (
    InForce,
    InForceKind,
    OutputDocument,
    Power,
    PowerChange,
    PowerMethod,
    PowerRequest,
    PowerResult,
    decode_output_document,
    encode_output_report,
    output_document_key,
)
from contracts.node_output import OutputReport as WireReport

SERIAL = "pi-power"
ON_BY_TIMETABLE = InForce(InForceKind.TIMETABLE, Power.ON, None, None, None)


class FixedClock:
    """A TransactionClock held at `now` (Central's clock in a test)."""

    def __init__(self, now: float) -> None:
        self.now = now

    def now_in(self, conn) -> float:
        return self.now


def _projected(registry, times=None) -> OutputDocument:
    """HDMI-A-1's document as the worker's source projects (and records) it now."""
    source = DisplayDocuments(PgLinkStores(registry.db), DisplayWakes(),
                              times=times or DatabaseTransactionClock()).source(SERIAL, Pipe.FLEET)
    return decode_output_document(asyncio.run(source.documents(DOCUMENT_STREAM))[output_document_key("HDMI-A-1")])


def _test(registry, test_id: str) -> InForce:
    return InForce(InForceKind.TEST, Power.OFF, test_id, 299, None)


def _answer(registry, for_change: int, result: PowerResult, in_force: InForce | None) -> None:
    """The Pi's report on HDMI-A-1 for `for_change`, recorded through the link store and its judge."""
    report = WireReport("HDMI-A-1", True, NO_SERIAL, (), (PowerMethod.DDC_CI,), PowerMethod.DDC_CI, for_change,
                        result, in_force, None)
    _report(registry, SERIAL, "HDMI-A-1", NO_SERIAL, data=encode_output_report(report))


def _bound_frame(registry) -> tuple[str, str]:
    player = enroll_pi(registry, SERIAL)
    frame = _frame(registry, "kitchen")
    _bind(registry, frame, player, "HDMI-A-1")
    return frame, player


def _rows(registry, sql: str, params=()) -> list[dict]:
    with registry.db.transaction() as conn:
        cursor = conn.execute(sql, params)
        return cursor.fetchall() if cursor.description else []


def test_a_test_off_goes_on_top_of_standing_on_and_turn_on_ends_it(registry):
    """Owner q1 (2026-10-10): a test lasts "5 minutes, or less if you press Turn on. When the test
    ends, the request underneath takes over." Turn on ends the live test; it puts nothing on top."""
    frame, _ = _bound_frame(registry)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        assert _projected(registry).power.test is None

        response = client.post(f"/v1/operator/frames/{frame}/power-tests", headers=AUTH)
        assert response.status_code == 202
        accepted = response.json()
        assert accepted["for_seconds"] == TEST_SECONDS
        off = _projected(registry)
        assert off.power.test == PowerRequest(accepted["request_id"], Power.OFF, TEST_SECONDS)
        assert off.power.now is Power.ON                          # the timetable underneath
        assert off.change == 2

        turned_on = client.delete(f"/v1/operator/frames/{frame}/power-tests", headers=AUTH)
        assert turned_on.status_code == 204
        assert _rows(registry, "SELECT id FROM power_requests") == []
        on = _projected(registry)
        assert (on.power.test, on.change) == (None, 3)            # the timetable underneath takes over

        # With no live test there is nothing to end: the request underneath is already in force.
        assert client.delete(f"/v1/operator/frames/{frame}/power-tests", headers=AUTH).status_code == 204
        assert _projected(registry).change == 3

        assert client.post(f"/v1/operator/frames/{frame}/power-tests").status_code == 401
        assert client.delete(f"/v1/operator/frames/{frame}/power-tests").status_code == 401
        assert client.delete("/v1/operator/frames/nowhere/power-tests", headers=AUTH).json() == {
            "error": "unknown_frame"}


def test_after_turn_on_the_tab_reads_the_pis_answer_for_the_request_underneath(registry):
    """The tab's result is the Pi's for the request Central has in force: after Turn on, none until
    the Pi's report names the request underneath on the change that dropped the test."""
    frame, _ = _bound_frame(registry)
    _report(registry, SERIAL, "HDMI-A-1", NO_SERIAL)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        def power() -> dict:
            return client.get(f"/v1/operator/frames/{frame}/power", headers=AUTH).json()

        test_id = client.post(f"/v1/operator/frames/{frame}/power-tests", headers=AUTH).json()["request_id"]
        _answer(registry, _projected(registry).change, PowerResult.CONFIRMED, _test(registry, test_id))
        assert (power()["status"], power()["result"]) == ("answered", "confirmed")

        client.delete(f"/v1/operator/frames/{frame}/power-tests", headers=AUTH)
        dropped = _projected(registry).change
        ending = power()                                           # the Pi still names the test
        assert (ending["status"], ending["test"], ending["result"]) == ("idle", None, None)
        assert ending["in_force"]["kind"] == "test"                # what the Pi last said, as it said it

        _answer(registry, dropped, PowerResult.DID_NOT_ANSWER, ON_BY_TIMETABLE)
        answered = power()
        assert (answered["status"], answered["test"], answered["result"]) == ("idle", None, "did-not-answer")
        assert (answered["in_force"]["kind"], answered["in_force"]["power"]) == ("timetable", "on")


def test_a_test_is_refused_for_an_unknown_or_unbound_frame(registry):
    enroll_pi(registry, SERIAL)
    frame = _frame(registry, "hallway")
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        unbound = client.post(f"/v1/operator/frames/{frame}/power-tests", headers=AUTH)
        assert (unbound.status_code, unbound.json()) == (409, {"error": "frame_unbound"})
        unknown = client.post("/v1/operator/frames/nowhere/power-tests", headers=AUTH)
        assert (unknown.status_code, unknown.json()) == (404, {"error": "unknown_frame"})
        assert client.get("/v1/operator/frames/nowhere/power", headers=AUTH).status_code == 404
        idle = client.get(f"/v1/operator/frames/{frame}/power", headers=AUTH).json()
        assert (idle["status"], idle["output_id"], idle["test"]) == ("unbound", None, None)
    assert _rows(registry, "SELECT id FROM power_requests") == []


def test_past_its_end_the_test_leaves_the_projection_and_the_change_rises(registry):
    frame, _ = _bound_frame(registry)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        client.post(f"/v1/operator/frames/{frame}/power-tests", headers=AUTH)
    assert _projected(registry).change == 1 and _projected(registry).power.test is not None

    # The source's changed() returns at the earliest live end of its Node's tests, with no wake.
    _rows(registry, "UPDATE power_requests SET ends_at = EXTRACT(EPOCH FROM clock_timestamp()) + 1")
    source = DisplayDocuments(PgLinkStores(registry.db), DisplayWakes(),
                              times=DatabaseTransactionClock()).source(SERIAL, Pipe.FLEET)

    async def ended() -> None:
        await asyncio.wait_for(source.changed(), 5)
    asyncio.run(ended())

    after = _projected(registry)
    assert (after.power.test, after.change) == (None, 2)
    assert _projected(registry).change == 2


def test_the_power_tab_waits_for_the_report_on_the_change_carrying_the_test(registry):
    frame, player = _bound_frame(registry)
    _report(registry, SERIAL, "HDMI-A-1", NO_SERIAL)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        def power() -> dict:
            response = client.get(f"/v1/operator/frames/{frame}/power", headers=AUTH)
            assert response.status_code == 200
            return response.json()

        before = _projected(registry).change
        _answer(registry, before, PowerResult.CONFIRMED, ON_BY_TIMETABLE)
        idle = power()
        assert (idle["status"], idle["test"], idle["result"], idle["player_id"], idle["output_id"]) == (
            "idle", None, "confirmed", player, "HDMI-A-1")   # the timetable underneath, answered
        assert idle["in_force"] == {"kind": "timetable", "power": "on", "request_id": None,
                                    "remaining_seconds": None, "since": None}

        test_id = client.post(f"/v1/operator/frames/{frame}/power-tests",
                              headers=AUTH).json()["request_id"]
        assert power()["status"] == "waiting"                      # no document carries it yet
        carrying = _projected(registry).change
        assert carrying == before + 1

        waiting = power()                                          # the Pi still reports the older change
        assert (waiting["status"], waiting["result"], waiting["test"]["request_id"]) == ("waiting", None, test_id)
        assert waiting["test"]["for_seconds"] == TEST_SECONDS

        _answer(registry, carrying, PowerResult.DID_NOT_ANSWER, _test(registry, test_id))
        answered = power()
        assert (answered["status"], answered["result"]) == ("answered", "did-not-answer")
        assert (answered["answers"], answered["method_in_use"]) == (["ddc-ci"], "ddc-ci")
        assert answered["in_force"]["request_id"] == test_id
        assert (answered["in_force"]["kind"], answered["in_force"]["power"]) == ("test", "off")
        assert answered["display_id"] is not None


def test_the_power_tab_counts_down_on_centrals_clock_and_drops_an_ended_test(registry):
    """The Pi reports only on a change, so its count stands still: the tab's count of the live test
    is Central's (ends_at against Central's clock). Past ends_at the tab shows no test, even while the
    last document still carries it (model.live_test, the one rule the projection also reads)."""
    frame, _ = _bound_frame(registry)
    _report(registry, SERIAL, "HDMI-A-1", NO_SERIAL)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        test_id = client.post(f"/v1/operator/frames/{frame}/power-tests",
                              headers=AUTH).json()["request_id"]
        _answer(registry, _projected(registry).change, PowerResult.CONFIRMED,
                InForce(InForceKind.TEST, Power.OFF, test_id, 300, None))
        _rows(registry, "UPDATE power_requests SET ends_at = EXTRACT(EPOCH FROM clock_timestamp()) + 90")
        counted = client.get(f"/v1/operator/frames/{frame}/power", headers=AUTH).json()
        assert counted["status"] == "answered" and counted["test"]["request_id"] == test_id
        assert counted["in_force"]["remaining_seconds"] in (89, 90)      # not the Pi's 300

        _rows(registry, "UPDATE power_requests SET ends_at = EXTRACT(EPOCH FROM clock_timestamp()) - 1")
        ended = client.get(f"/v1/operator/frames/{frame}/power", headers=AUTH).json()
        assert (ended["status"], ended["test"], ended["result"]) == ("idle", None, None)


def test_last_heard_is_when_centrals_hub_saw_the_pis_link_go(registry):
    """Not the last report's time: the Pi puts a report only when it changes, hours apart on a quiet
    display. While linked (or never seen by the hub) there is no last-heard time to show."""
    frame, _ = _bound_frame(registry)
    _report(registry, SERIAL, "HDMI-A-1", NO_SERIAL)
    device = device_id_for_serial(SERIAL)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        def power() -> dict:
            return client.get(f"/v1/operator/frames/{frame}/power", headers=AUTH).json()

        assert (power()["linked"], power()["last_heard_at"]) == (None, None)
        _rows(registry, "INSERT INTO node_bus_presence(device_id, linked, changed_at) VALUES (%s, true, 5)", (device,))
        assert (power()["linked"], power()["last_heard_at"]) == (True, None)
        _rows(registry, "UPDATE node_bus_presence SET linked=false, changed_at=1791000123.5 WHERE device_id=%s",
              (device,))
        assert (power()["linked"], power()["last_heard_at"]) == (False, 1791000123.5)


def test_each_command_wakes_the_worker_through_a_notification(registry):
    frame, _ = _bound_frame(registry)
    _report(registry, SERIAL, "HDMI-A-1", NO_SERIAL)
    device = device_id_for_serial(SERIAL)
    wakes = DisplayWakes()
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        display_id = client.get(f"/v1/operator/frames/{frame}/power", headers=AUTH).json()["display_id"]

        async def woken_by(command) -> None:
            listening = asyncio.create_task(wakes.listen(registry.db.dsn))
            try:
                seen = await asyncio.wait_for(wakes.after(device, wakes.count(device)), 5)   # it listens
                await asyncio.to_thread(command)
                await asyncio.wait_for(wakes.after(device, seen), 5)
            finally:
                listening.cancel()

        asyncio.run(woken_by(lambda: client.post(f"/v1/operator/frames/{frame}/power-tests",
                                                 headers=AUTH)))
        asyncio.run(woken_by(lambda: client.delete(f"/v1/operator/frames/{frame}/power-tests",
                                                   headers=AUTH)))
        asyncio.run(woken_by(lambda: client.put(f"/v1/operator/displays/{display_id}/power-settings",
                                                headers=AUTH, json={"power_method": "signal-off",
                                                                    "switch_input_on_power_on": True,
                                                                    "never_off_on_other_input": False})))


def test_power_settings_change_the_output_document(registry):
    frame, _ = _bound_frame(registry)
    _report(registry, SERIAL, "HDMI-A-1", NO_SERIAL)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        display_id = client.get(f"/v1/operator/frames/{frame}/power", headers=AUTH).json()["display_id"]
        settings = {"power_method": "hdmi-cec", "switch_input_on_power_on": False, "never_off_on_other_input": False}
        response = client.put(f"/v1/operator/displays/{display_id}/power-settings", headers=AUTH, json=settings)
        assert (response.status_code, response.json()) == (200, settings)
        document = _projected(registry)
        assert (document.method, document.switch_input_on_power_on, document.never_off_on_other_input) == (
            PowerMethod.HDMI_CEC, False, False)
        tab = client.get(f"/v1/operator/frames/{frame}/power", headers=AUTH).json()
        assert {key: tab[key] for key in settings} == settings
        unknown = client.put(f"/v1/operator/displays/{uuid.uuid4()}/power-settings", headers=AUTH, json=settings)
        assert (unknown.status_code, unknown.json()) == (404, {"error": "unknown_display"})


def _london(at: str) -> float:
    return datetime.fromisoformat(at).replace(tzinfo=ZoneInfo("Europe/London")).timestamp()


def test_the_document_carries_the_frames_timetable_and_its_change_moves_only_at_set_times(registry):
    """A9: the Frame follows Night off (23:00-07:00, seeded by migration 089) in a Europe/London
    house. The document's power part is its timetable in calendar set times; reading it again at
    any moment between two set times gives the same change number; it rises at 23:00 (the power
    now is off) and at house midnight (the horizon moves); the Power tab names the set time the
    Pi says is in force on the house clock."""
    frame, _ = _bound_frame(registry)
    set_house(registry.db, LONDON)
    _rows(registry, "UPDATE night_off SET settings = jsonb_set(settings, '{frames}', "
                    "jsonb_build_object('kind', 'frames', 'frame_ids', jsonb_build_array(%s::text)))", (frame,))
    clock = FixedClock(_london("2026-10-12T16:00"))
    first = _projected(registry, clock)
    assert first.power.now is Power.ON and first.power.test is None
    assert first.power.changes[:2] == (PowerChange("2026-10-12T22:00:00Z", Power.OFF),
                                       PowerChange("2026-10-13T06:00:00Z", Power.ON))
    assert first.power.until == "2026-10-19T23:00:00Z"              # the start of house day 20 Oct
    for later in ("2026-10-12T16:00:01", "2026-10-12T20:00", "2026-10-12T22:59:59"):
        clock.now = _london(later)
        assert _projected(registry, clock).change == first.change   # Central's clock is not in it
    clock.now = _london("2026-10-12T23:00")
    night = _projected(registry, clock)
    assert (night.change, night.power.now, night.power.changes[0].at) == (
        first.change + 1, Power.OFF, "2026-10-13T06:00:00Z")
    clock.now = _london("2026-10-12T23:59:59")
    assert _projected(registry, clock).change == night.change
    clock.now = _london("2026-10-13T00:00")
    midnight = _projected(registry, clock)
    assert (midnight.change, midnight.power.until) == (night.change + 1, "2026-10-20T23:00:00Z")

    _answer(registry, midnight.change, PowerResult.CONFIRMED,
            InForce(InForceKind.TIMETABLE, Power.OFF, None, None, "2026-10-12T22:00:00Z"))
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        tab = client.get(f"/v1/operator/frames/{frame}/power", headers=AUTH).json()
        assert tab["in_force"] == {"kind": "timetable", "power": "off", "request_id": None,
                                   "remaining_seconds": None, "since": "2026-10-12T23:00"}
        night_off = client.get("/v1/operator/power/night-off", headers=AUTH)
        assert night_off.status_code == 200
        assert night_off.json()["night_off"]["frames"] == {"kind": "frames", "frame_ids": [frame]}


def test_the_document_source_wakes_at_the_next_set_time_and_at_a_hold_end_that_changes_power(registry):
    """The worker's document source sleeps until the Node's next deadline, so a set time or the
    end of a hold reaches the Pi with no command (central/infra/display_store.py
    `_until_next_deadline`). At 22:00 in London the Frame's Night off starts at 23:00, before
    house midnight; an "off" hold until 22:30 brings the wake forward to its end (on again).

    Mutation probe: drop the timetable's next change from the deadlines -> 7200 s (midnight)."""
    frame, _ = _bound_frame(registry)
    set_house(registry.db, LONDON)
    now = _london("2026-10-12T22:00")
    _rows(registry, "UPDATE night_off SET settings = jsonb_set(settings, '{frames}', "
                    "jsonb_build_object('kind', 'frames', 'frame_ids', jsonb_build_array(%s::text)))", (frame,))
    device = device_id_for_serial(SERIAL)
    with registry.db.transaction() as conn:
        assert _until_next_deadline(conn, device, now) == 3600          # Night off at 23:00
        conn.execute("INSERT INTO power_holds (frame_id, hold_id, power, source, started_at, ends_at) "
                     "VALUES (%s, %s, 'off', 'console', %s, %s)", (frame, str(uuid.uuid4()), now, now + 1800))
        assert _until_next_deadline(conn, device, now) == 1800          # the hold's end: on again
