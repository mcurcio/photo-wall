"""Settings › House (Power and the week, roadmap 2b A4; settings #2-#5), through GET and PUT
/v1/operator/house on a test clock: with no row the house is Central's own zone, "cm", "24h"; an
unknown zone is refused; a saved zone and Place move Night off's set times in the Output document
the worker's source projects and every Program's windows, and wake every Node's document source; a
sunset When is "needs-place" until the house has a Place.

Mutation probes: (P-zone-ignored) keep reading the seed zone after save (the save does not store
the zone) -> the moved-set-time assertion fails; (P-unconfirmed-drives) Night off in force before
the zone is confirmed -> a 23:00 change before the first save.
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from support.house_time import EVERY_DAY, LONDON, at
from test_display_identity import ADMIN, AUTH, NO_SERIAL, _report
from test_display_power_requests import SERIAL, FixedClock, _bound_frame, _projected, _rows

from central.app import create_app
from central.content_catalog.catalog import device_id_for_serial
from central.house_time.model import HouseTime
from central.infra.display_store import DisplayWakes
from central.infra.house_store import server_zone
from central.runtime import Contribution, Scene

NEW_YORK = HouseTime(zone="America/New_York")
PLACE = {"latitude": 40.71, "longitude": -74.01}
SUNSET = {"days": list(EVERY_DAY), "start": {"at": "sunset", "offset_minutes": -30},
          "end": {"at": "clock", "time": "23:30"}}


def _house(zone: str, place=None) -> dict:
    return {"time": {"zone": zone, "place": place}, "units": "in", "clock": "12h"}


def test_with_no_row_the_house_is_centrals_own_zone(registry):
    client = TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False))   # no start: no seed
    assert _rows(registry, "SELECT zone FROM house_settings") == []
    response = client.get("/v1/operator/house", headers=AUTH)
    assert response.status_code == 200, response.text
    assert response.json() == {"time": {"zone": server_zone(), "place": None}, "units": "cm", "clock": "24h",
                               "updated_at": 0.0, "zone_confirmed": False}
    assert client.get("/v1/operator/house").status_code == 401
    assert client.put("/v1/operator/house", json=_house("Europe/London")).status_code == 401


def test_an_unknown_zone_is_refused(registry):
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        before = client.get("/v1/operator/house", headers=AUTH).json()
        refused = client.put("/v1/operator/house", headers=AUTH, json=_house("Mars/Base"))
        assert (refused.status_code, refused.json()) == (422, {"error": "unknown-zone"})
        malformed = client.put("/v1/operator/house", headers=AUTH, json={**_house("Europe/London"), "units": "km"})
        assert (malformed.status_code, malformed.json()) == (422, {"error": "invalid_request"})
        assert client.get("/v1/operator/house", headers=AUTH).json() == before


def test_a_saved_zone_and_place_move_night_off_and_the_programs_windows(registry):
    frame, _ = _bound_frame(registry)
    _report(registry, SERIAL, "HDMI-A-1", NO_SERIAL)
    _rows(registry, "UPDATE night_off SET settings = jsonb_set(settings, '{frames}', "
                    "jsonb_build_object('kind', 'frames', 'frame_ids', jsonb_build_array(%s::text)))", (frame,))
    noon = at(LONDON, "2026-10-12T12:00")
    registry.clock.advance(noon - registry.clock.utc())
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    app.state.coordinator.runtime.command("set_scene", Scene(scene_id="evening", cycle_seconds=600, loop=True,
                                                             contributions=(Contribution(target=f"frame:{frame}",
                                                                                         source_refs=("library:1",)),)))
    device = device_id_for_serial(SERIAL)
    wakes = DisplayWakes()
    clock = FixedClock(at(LONDON, "2026-10-12T16:00"))
    with TestClient(app) as client:
        # Until the operator confirms the zone (Central's own here), Night off switches nothing.
        assert client.get("/v1/operator/power/night-off", headers=AUTH).json()["waits_for_zone"] is True
        assert _projected(registry, clock).power.changes == ()
        assert client.put("/v1/operator/house", headers=AUTH, json=_house("Europe/London")).status_code == 200
        assert client.get("/v1/operator/power/night-off", headers=AUTH).json()["waits_for_zone"] is False
        refused = client.post("/v1/operator/programs", headers=AUTH, json={
            "name": "Sunset", "scene_id": "evening", "when": SUNSET})
        assert (refused.status_code, refused.json()) == (422, {"error": "needs-place"})
        created = client.post("/v1/operator/programs", headers=AUTH, json={
            "name": "Evenings", "scene_id": "evening", "when": {
                "days": list(EVERY_DAY), "start": {"at": "clock", "time": "17:00"},
                "end": {"at": "clock", "time": "23:00"}}})
        assert created.status_code == 201, created.text
        program_id = created.json()["program_id"]

        def next_window() -> dict:
            body = client.get("/v1/operator/runtime", headers=AUTH).json()
            return body["program_windows"][program_id]["next"]

        assert _projected(registry, clock).power.changes[0].at == "2026-10-12T22:00:00Z"   # 23:00 BST
        assert next_window()["start_at"] == at(LONDON, "2026-10-12T17:00")

        async def woken_by(command):
            listening = asyncio.create_task(wakes.listen(registry.db.dsn))
            try:
                seen = await asyncio.wait_for(wakes.after(device, wakes.count(device)), 5)   # it listens
                answer = await asyncio.to_thread(command)
                await asyncio.wait_for(wakes.after(device, seen), 5)
                return answer
            finally:
                listening.cancel()

        saved = asyncio.run(woken_by(lambda: client.put("/v1/operator/house", headers=AUTH,
                                                        json=_house("America/New_York", PLACE))))
        assert saved.status_code == 200, saved.text
        assert saved.json() == {**_house("America/New_York", PLACE), "updated_at": noon, "zone_confirmed": True}
        assert client.get("/v1/operator/house", headers=AUTH).json() == saved.json()
        assert _projected(registry, clock).power.changes[0].at == "2026-10-13T03:00:00Z"   # 23:00 EDT
        assert next_window()["start_at"] == at(NEW_YORK, "2026-10-12T17:00")
        assert next_window()["start"] == "2026-10-12T17:00"

        sunset = client.post("/v1/operator/programs", headers=AUTH, json={
            "name": "Sunset", "scene_id": "evening", "when": SUNSET})
        assert sunset.status_code == 201, sunset.text

        # A Schedule uses sunset now: the location cannot be cleared (it would silently stop),
        # and the house is left as it was.
        in_use = client.put("/v1/operator/house", headers=AUTH, json=_house("America/New_York"))
        assert (in_use.status_code, in_use.json()) == (409, {"error": "place-in-use"})
        assert client.get("/v1/operator/house", headers=AUTH).json() == saved.json()


def test_the_location_cannot_go_while_night_off_uses_sunset(registry):
    """Mutation probe: skip the place-in-use check -> the location is cleared and Night off
    stops turning displays off without a word."""
    _rows(registry, "UPDATE night_off SET settings = jsonb_set(settings, '{off}', "
                    "'{\"at\": \"sunset\", \"offset_minutes\": 0}'::jsonb)")
    with TestClient(create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)) as client:
        assert client.put("/v1/operator/house", headers=AUTH,
                          json=_house("America/New_York", PLACE)).status_code == 200
        refused = client.put("/v1/operator/house", headers=AUTH, json=_house("America/New_York"))
        assert (refused.status_code, refused.json()) == (409, {"error": "place-in-use"})
        moved = client.put("/v1/operator/house", headers=AUTH, json=_house("Europe/London", PLACE))
        assert moved.status_code == 200, moved.text
