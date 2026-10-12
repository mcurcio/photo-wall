"""Night off and holds through the operator API (roadmap 2a; Power and the week, slice S2).

Every command goes through the routes the Power lane calls (`create_app` with Central's clock held
where the test puts it); every answer is read back through the Output document the worker's source
projects (`DisplayDocuments` on the same clock). The house is Europe/London; one Pi has two HDMI
ports: "living" on HDMI-A-1 follows Night off (23:00 to 07:00, every day), "hall" on HDMI-A-2 does
not.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from support.house_time import LONDON, at, set_house
from test_display_identity import ADMIN, AUTH, _bind, _frame, enroll_pi

from central.app import create_app
from central.content_catalog.catalog import device_id_for_serial
from central.infra.display_store import DOCUMENT_STREAM, DisplayDocuments, DisplayWakes
from central.infra.node_link_store import PgLinkStores
from contracts.node_link import Pipe
from contracts.node_output import (
    OutputDocument,
    Power,
    decode_output_document,
    decode_set_time,
    output_document_key,
)

SERIAL = "pi-night-off"
LIVING, HALL = "living", "hall"
PORTS = {LIVING: "HDMI-A-1", HALL: "HDMI-A-2"}
NIGHT_OFF = "/v1/operator/power/night-off"
HOLDS = "/v1/operator/power/holds"


def local(text: str) -> float:
    return at(LONDON, text)


THREE_PM = local("2026-10-12T15:00")


class FixedClock:
    """Central's TransactionClock in this test: held where the test puts it."""

    def __init__(self, now: float) -> None:
        self.now = now

    def now_in(self, conn: Any) -> float:
        return self.now


def draft(*, off: str = "23:00", on: str = "07:00", days: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri",
                                                                             "sat", "sun"),
          frames: tuple[str, ...] = (LIVING,), hold_lasts: str = "next-scheduled-change") -> dict:
    return {"off": {"at": "clock", "time": off}, "on": {"at": "clock", "time": on}, "days": list(days),
            "frames": {"kind": "frames", "frame_ids": sorted(frames)}, "darkness": "display-off",
            "hold_lasts": hold_lasts}


class House:
    """The test's house: the API on Central's fixed clock and each Frame's projected document."""

    def __init__(self, registry, client: TestClient, clock: FixedClock) -> None:
        self.registry, self.client, self.clock = registry, client, clock

    def save(self, revision: int, **values) -> Any:
        return self.client.put(NIGHT_OFF, headers=AUTH, json={"expected_revision": revision,
                                                              "night_off": draft(**values)})

    def hold(self, power: str, request_id: uuid.UUID) -> dict:
        response = self.client.post(f"/v1/operator/power/all-{power}", headers=AUTH,
                                    json={"request_id": str(request_id)})
        assert response.status_code == 201, response.text
        return response.json()

    def resume(self, *frame_ids: str) -> None:
        response = self.client.post("/v1/operator/power/resume", headers=AUTH, json={"frame_ids": list(frame_ids)})
        assert response.status_code == 204, response.text

    def holds(self) -> list[dict]:
        return self.client.get(HOLDS, headers=AUTH).json()

    def document(self, frame_id: str) -> OutputDocument:
        source = DisplayDocuments(PgLinkStores(self.registry.db), DisplayWakes(), times=self.clock).source(
            SERIAL, Pipe.FLEET)
        documents = asyncio.run(source.documents(DOCUMENT_STREAM))
        return decode_output_document(documents[output_document_key(PORTS[frame_id])])

    def wanted(self, frame_id: str) -> tuple[Power, tuple[tuple[float, Power], ...]]:
        """The document's `now` and its first two set times (UTC seconds, power)."""
        power = self.document(frame_id).power
        return power.now, tuple((decode_set_time(change.at), change.power) for change in power.changes[:2])


@pytest.fixture
def house(registry):
    set_house(registry.db, LONDON)
    player = enroll_pi(registry, SERIAL)
    for frame_id, port in PORTS.items():
        _bind(registry, _frame(registry, frame_id), player, port)
    clock = FixedClock(THREE_PM)
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False, media_times=clock)) as client:
        subject = House(registry, client, clock)
        assert subject.save(1).status_code == 200            # Night off on "living" only
        yield subject


def test_all_off_holds_every_frame_until_night_offs_next_change_and_the_newest_hold_wins(house):
    """Mutation probe (P-hold-per-frame-end): end a hold at that Frame's own next change -> "hall",
    outside Night off, stays held past 23:00."""
    held = house.hold("off", uuid.uuid4())
    assert (held["power"], held["frame_ids"], held["until"]) == ("off", [HALL, LIVING], "2026-10-12T23:00")
    assert held["until_at"] == local("2026-10-12T23:00") and held["started_at"] == THREE_PM
    # "living": held off, then Night off from 23:00 (no change), on at 07:00.
    assert house.wanted(LIVING) == (Power.OFF, ((local("2026-10-13T07:00"), Power.ON),
                                                (local("2026-10-13T23:00"), Power.OFF)))
    # "hall" is held to the same moment, then back to on.
    assert house.wanted(HALL) == (Power.OFF, ((local("2026-10-12T23:00"), Power.ON),))

    house.clock.now = local("2026-10-12T15:05")
    newest = house.hold("on", uuid.uuid4())
    assert [hold["hold_id"] for hold in house.holds()] == [newest["hold_id"]]
    assert house.wanted(LIVING) == (Power.ON, ((local("2026-10-12T23:00"), Power.OFF),
                                               (local("2026-10-13T07:00"), Power.ON)))
    assert house.wanted(HALL)[0] is Power.ON

    house.clock.now = local("2026-10-12T23:00") + 1        # the hold ended: the schedule resumes
    assert house.holds() == []
    assert house.wanted(LIVING) == (Power.OFF, ((local("2026-10-13T07:00"), Power.ON),
                                                (local("2026-10-13T23:00"), Power.OFF)))
    assert house.wanted(HALL) == (Power.ON, ())


def test_resume_schedule_ends_a_hold_at_once_and_wakes_every_node(house):
    device = device_id_for_serial(SERIAL)
    wakes = DisplayWakes()

    async def woken_by(command) -> None:
        listening = asyncio.create_task(wakes.listen(house.registry.db.dsn))
        try:
            seen = await asyncio.wait_for(wakes.after(device, wakes.count(device)), 5)   # it listens
            await asyncio.to_thread(command)
            await asyncio.wait_for(wakes.after(device, seen), 5)
        finally:
            listening.cancel()

    asyncio.run(woken_by(lambda: house.hold("off", uuid.uuid4())))
    assert house.wanted(LIVING)[0] is Power.OFF
    asyncio.run(woken_by(lambda: house.resume(LIVING)))
    assert house.wanted(LIVING) == (Power.ON, ((local("2026-10-12T23:00"), Power.OFF),
                                               (local("2026-10-13T07:00"), Power.ON)))
    assert [hold["frame_ids"] for hold in house.holds()] == [[HALL]]
    assert house.wanted(HALL)[0] is Power.OFF
    asyncio.run(woken_by(lambda: house.hold("on", uuid.uuid4())))
    asyncio.run(woken_by(lambda: house.save(2, off="22:00")))
    house.resume(LIVING, HALL, "nowhere")          # a Frame with no hold is no error
    assert house.holds() == []


def test_the_same_request_twice_makes_one_hold_that_keeps_its_start(house):
    """Mutation probe (P-idempotent): drop the `hold_id <>` guard -> the double tap restarts
    `started_at`."""
    request = uuid.uuid4()
    first = house.hold("off", request)
    house.clock.now = THREE_PM + 2
    again = house.hold("off", request)
    assert again == first
    (hold,) = house.holds()
    assert (hold["hold_id"], hold["started_at"], hold["frame_ids"]) == (str(request), THREE_PM, [HALL, LIVING])


def test_a_hold_until_resumed_survives_every_scheduled_change(house):
    assert house.save(2, hold_lasts="until-resumed").status_code == 200
    held = house.hold("on", uuid.uuid4())
    assert (held["until_at"], held["until"]) == (None, None)
    for moment in ("2026-10-12T23:30", "2026-10-13T07:30", "2026-10-15T23:30"):
        house.clock.now = local(moment)
        assert house.wanted(LIVING) == (Power.ON, ()), moment
        assert [hold["hold_id"] for hold in house.holds()] == [held["hold_id"]]
    house.resume(LIVING)
    assert house.wanted(LIVING)[0] is Power.OFF       # 23:30: Night off again


def test_an_expired_hold_reads_as_gone_with_no_reaper(house):
    house.hold("off", uuid.uuid4())
    house.clock.now = local("2026-10-12T23:00")
    assert house.holds() == []
    with house.registry.db.transaction() as conn:   # still stored: the next command deletes it
        assert conn.execute("SELECT count(*) AS n FROM power_holds").fetchone()["n"] == 2
    house.hold("on", uuid.uuid4())
    with house.registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM power_holds WHERE power='off'").fetchone()["n"] == 0


def test_night_off_saves_refuse_a_stale_revision_no_days_and_an_unknown_frame(house):
    """Mutation probe (P-revision): skip the revision check -> the stale save is stored."""
    stored = house.client.get(NIGHT_OFF, headers=AUTH).json()
    assert stored["night_off"]["hold_lasts"] == "next-scheduled-change"     # the #90 default
    assert stored["night_off"]["revision"] == 2
    for response, status, code in (
            (house.save(1, off="21:00"), 409, "changed"),
            (house.save(2, days=()), 422, "no-days"),
            (house.save(2, frames=(LIVING, "nowhere")), 422, "unknown-frame"),
            (house.client.put(NIGHT_OFF, headers=AUTH, json={"expected_revision": 2, "night_off": {
                **draft(), "off": {"at": "sunset", "offset_minutes": 0}}}), 422, "needs-place")):
        assert (response.status_code, response.json()) == (status, {"error": code})
    assert house.client.get(NIGHT_OFF, headers=AUTH).json() == stored

    saved = house.save(2, off="22:00")
    assert saved.status_code == 200
    assert saved.json()["night_off"]["revision"] == 3 and saved.json()["next_off"] == "2026-10-12T22:00"
    assert house.wanted(LIVING) == (Power.ON, ((local("2026-10-12T22:00"), Power.OFF),
                                               (local("2026-10-13T07:00"), Power.ON)))
