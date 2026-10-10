"""Test off and on as a timed power request in the Output document (roadmap 1b, slice C3; owner q1,
2026-10-10: "5 minutes, or less if Turn on is pressed").

Every request goes through the operator API that the Power tab calls; every Output report travels
the link store's commit and its judge (test_display_identity's helpers); the projection is the
document source the worker's NodeLinks read. (1) A test off on a bound Frame answers 202 and the
projection carries it on top of standing on. (2) Turn on replaces it: one row. (3) An unbound Frame
is refused. (4) Past `ends_at` the projection drops the test and the change rises; the source's
`changed()` returns at that end. (5) The Power tab's status waits until a report names the change
carrying the test, then answers with its result; a report naming an older change keeps waiting.
(6) Each command wakes the worker through PostgreSQL's notification. (7) The power settings change
the document.
"""
from __future__ import annotations

import asyncio
import uuid

from fastapi.testclient import TestClient
from test_display_identity import ADMIN, AUTH, NO_SERIAL, _bind, _frame, _report, enroll_pi

from central.app import create_app
from central.content_catalog.catalog import device_id_for_serial
from central.displays.model import TEST_SECONDS
from central.infra.display_store import DOCUMENT_STREAM, DisplayDocuments, DisplayWakes
from central.infra.node_link_store import PgLinkStores
from contracts.node_link import Pipe
from contracts.node_output import (
    InForce,
    OutputDocument,
    Power,
    PowerMethod,
    PowerRequest,
    PowerResult,
    RequestReason,
    decode_output_document,
    encode_output_report,
    output_document_key,
)
from contracts.node_output import OutputReport as WireReport

SERIAL = "pi-power"
STANDING = PowerRequest("standing", Power.ON, RequestReason.STANDING)


def _projected(registry) -> OutputDocument:
    """HDMI-A-1's document as the worker's source projects (and records) it now."""
    source = DisplayDocuments(PgLinkStores(registry.db), DisplayWakes()).source(SERIAL, Pipe.FLEET)
    return decode_output_document(asyncio.run(source.documents(DOCUMENT_STREAM))[output_document_key("HDMI-A-1")])


def _answer(registry, for_change: int, result: PowerResult, in_force: InForce | None) -> None:
    """The Pi's report on HDMI-A-1 for `for_change`, recorded through the link store and its judge."""
    report = WireReport("HDMI-A-1", True, NO_SERIAL, (), (PowerMethod.DDC_CI,), PowerMethod.DDC_CI, for_change,
                        result, in_force)
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


def test_a_test_off_goes_on_top_of_standing_on_and_turn_on_replaces_it(registry):
    frame, _ = _bound_frame(registry)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        assert _projected(registry).power == (STANDING,)

        response = client.post(f"/v1/operator/frames/{frame}/power-tests", headers=AUTH, json={"power": "off"})
        assert response.status_code == 202
        accepted = response.json()
        assert (accepted["power"], accepted["for_seconds"]) == ("off", TEST_SECONDS)
        off = _projected(registry)
        assert off.power == (PowerRequest(accepted["request_id"], Power.OFF, RequestReason.CONSOLE_TEST,
                                          TEST_SECONDS), STANDING)
        assert off.change == 2

        turned_on = client.post(f"/v1/operator/frames/{frame}/power-tests", headers=AUTH,
                                json={"power": "on"}).json()
        assert turned_on["request_id"] != accepted["request_id"]
        assert [(row["id"], row["power"]) for row in _rows(registry, "SELECT id, power FROM power_requests")] == [
            (uuid.UUID(turned_on["request_id"]), "on")]
        on = _projected(registry)
        assert on.power == (PowerRequest(turned_on["request_id"], Power.ON, RequestReason.CONSOLE_TEST,
                                         TEST_SECONDS), STANDING)
        assert on.change == 3

        assert client.post(f"/v1/operator/frames/{frame}/power-tests", json={"power": "off"}).status_code == 401


def test_a_test_is_refused_for_an_unknown_or_unbound_frame(registry):
    enroll_pi(registry, SERIAL)
    frame = _frame(registry, "hallway")
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        unbound = client.post(f"/v1/operator/frames/{frame}/power-tests", headers=AUTH, json={"power": "off"})
        assert (unbound.status_code, unbound.json()) == (409, {"error": "frame_unbound"})
        unknown = client.post("/v1/operator/frames/nowhere/power-tests", headers=AUTH, json={"power": "off"})
        assert (unknown.status_code, unknown.json()) == (404, {"error": "unknown_frame"})
        assert client.get("/v1/operator/frames/nowhere/power", headers=AUTH).status_code == 404
        idle = client.get(f"/v1/operator/frames/{frame}/power", headers=AUTH).json()
        assert (idle["status"], idle["output_id"], idle["test"]) == ("unbound", None, None)
    assert _rows(registry, "SELECT id FROM power_requests") == []


def test_past_its_end_the_test_leaves_the_projection_and_the_change_rises(registry):
    frame, _ = _bound_frame(registry)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        client.post(f"/v1/operator/frames/{frame}/power-tests", headers=AUTH, json={"power": "off"})
    assert _projected(registry).change == 1 and len(_projected(registry).power) == 2

    # The source's changed() returns at the earliest live end of its Node's tests, with no wake.
    _rows(registry, "UPDATE power_requests SET ends_at = EXTRACT(EPOCH FROM clock_timestamp()) + 1")
    source = DisplayDocuments(PgLinkStores(registry.db), DisplayWakes()).source(SERIAL, Pipe.FLEET)

    async def ended() -> None:
        await asyncio.wait_for(source.changed(), 5)
    asyncio.run(ended())

    after = _projected(registry)
    assert (after.power, after.change) == ((STANDING,), 2)
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
        _answer(registry, before, PowerResult.CONFIRMED, InForce("standing", None))
        idle = power()
        assert (idle["status"], idle["test"], idle["result"], idle["player_id"], idle["output_id"]) == (
            "idle", None, None, player, "HDMI-A-1")
        assert idle["in_force"] == {"request_id": "standing", "reason": "standing", "power": "on",
                                    "remaining_seconds": None}

        test_id = client.post(f"/v1/operator/frames/{frame}/power-tests", headers=AUTH,
                              json={"power": "off"}).json()["request_id"]
        assert power()["status"] == "waiting"                      # no document carries it yet
        carrying = _projected(registry).change
        assert carrying == before + 1

        waiting = power()                                          # the Pi still reports the older change
        assert (waiting["status"], waiting["result"], waiting["test"]["request_id"]) == ("waiting", None, test_id)
        assert waiting["test"]["for_seconds"] == TEST_SECONDS

        _answer(registry, carrying, PowerResult.DID_NOT_ANSWER, InForce(test_id, 299))
        answered = power()
        assert (answered["status"], answered["result"]) == ("answered", "did-not-answer")
        assert (answered["answers"], answered["method_in_use"]) == (["ddc-ci"], "ddc-ci")
        assert answered["in_force"] == {"request_id": test_id, "reason": "console-test", "power": "off",
                                        "remaining_seconds": 299}
        assert answered["display_id"] is not None and answered["last_heard_at"] is not None


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

        asyncio.run(woken_by(lambda: client.post(f"/v1/operator/frames/{frame}/power-tests", headers=AUTH,
                                                 json={"power": "off"})))
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
