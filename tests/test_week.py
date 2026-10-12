"""The week page's data (Power and the week, roadmap 2b E6): seven house days from a Monday,
worked out once in Central. From Monday 19 Oct 2026 in a Europe/London house the week crosses the
autumn change (Sunday 25 Oct has 25 hours): each Program's windows are in house time (the local
strings and the instants agree), each Frame following Night off has its off and on bands, and a
Frame outside Night off is one "on-underneath" band over the whole week. First the pure builder,
then GET /v1/operator/week through PostgreSQL.

Mutation probe (P-utc-days): cut days at UTC midnight -> the house-time day boundaries fail.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from support.house_time import EVERY_DAY, LONDON, at, daily, set_house
from test_display_identity import _frame
from test_operator_frames import ADMIN, AUTH

from central.app import create_app
from central.power.model import ChosenFrames, NightOff, PowerPolicy
from central.runtime import Contribution, Program, Runtime, Scene
from central.why.week import week
from contracts.node_output import Darkness

MONDAY = date(2026, 10, 19)
WEEK_START, WEEK_END = at(LONDON, "2026-10-19T00:00"), at(LONDON, "2026-10-26T00:00")


def _night_off(frame_ids: tuple[str, ...]) -> NightOff:
    return NightOff(off=daily("23:00", "07:00").start, on=daily("23:00", "07:00").end, days=EVERY_DAY,
                    frames=ChosenFrames(frame_ids=frame_ids), darkness=Darkness.DISPLAY_OFF, revision=1)


def _agrees(item: dict) -> None:
    """The local strings name the instants on the house clock."""
    for local, instant in ((item["start"], item["start_at"]), (item["end"], item["end_at"])):
        assert at(LONDON, local) == instant, item


def _check(body: dict) -> None:
    assert body["first_day"] == "2026-10-19" and body["zone"] == "Europe/London"
    assert WEEK_END - WEEK_START == 7 * 86400 + 3600                       # the Sunday has 25 hours
    windows = body["windows"]
    assert [(w["name"], w["start"], w["end"]) for w in windows] == [
        ("Evenings", f"{day}T17:00", f"{day}T23:00")
        for day in (str(MONDAY + timedelta(days=offset)) for offset in range(7))]
    for window in windows:
        _agrees(window)
    sunday = windows[-1]
    assert sunday["start_at"] == datetime(2026, 10, 25, 17, tzinfo=ZoneInfo("UTC")).timestamp()   # GMT now

    hall = [band for band in body["power"] if band["frame_id"] == "hall"]
    assert hall[0]["start_at"] == WEEK_START and hall[-1]["end_at"] == WEEK_END
    assert [(b["power"], b["because"], b["start"], b["end"]) for b in hall[:3]] == [
        ("off", "night-off", "2026-10-19T00:00", "2026-10-19T07:00"),
        ("on", "on-underneath", "2026-10-19T07:00", "2026-10-19T23:00"),
        ("off", "night-off", "2026-10-19T23:00", "2026-10-20T07:00")]
    assert len(hall) == 15 and hall[-1]["start"] == "2026-10-25T23:00"
    for band in hall:
        _agrees(band)
    (study,) = [band for band in body["power"] if band["frame_id"] == "study"]
    assert (study["power"], study["because"], study["start_at"], study["end_at"]) == (
        "on", "on-underneath", WEEK_START, WEEK_END)
    assert (study["hold_id"], study["source"]) == (None, None)


def test_the_week_builder_cuts_house_days_across_the_autumn_change():
    evenings = Program(program_id="evenings", name="Evenings", scene_id="evening", when=daily("17:00", "23:00"))
    built = week(programs={"evenings": evenings}, plays=Runtime(house=LONDON).program_windows_between,
                 default_scene_id="calm",
                 policy=PowerPolicy(_night_off(("hall",)), holds=(), zone_confirmed=True,
                                    hold_lasts="next-scheduled-change"), frame_ids=("study", "hall"), house=LONDON,
                 first_day=MONDAY)
    assert built.default_scene_id == "calm"
    _check(built.model_dump(mode="json"))


def test_get_week_serves_the_stored_programs_and_night_off(registry):
    set_house(registry.db, LONDON)
    for frame in ("hall", "study"):
        _frame(registry, frame)
    with registry.db.transaction() as conn:
        conn.execute("UPDATE night_off SET settings = jsonb_set(settings, '{frames}', "
                     "jsonb_build_object('kind', 'frames', 'frame_ids', jsonb_build_array('hall')))")
    registry.clock.advance(at(LONDON, "2026-10-12T12:00") - registry.clock.utc())
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    app.state.coordinator.runtime.command("set_scene", Scene(scene_id="evening", cycle_seconds=600, loop=True,
                                                             contributions=(Contribution(target="frame:hall",
                                                                                         source_refs=("x:1",)),)))
    with TestClient(app) as client:
        assert client.post("/v1/operator/programs", headers=AUTH, json={
            "name": "Evenings", "scene_id": "evening", "when": {
                "days": list(EVERY_DAY), "start": {"at": "clock", "time": "17:00"},
                "end": {"at": "clock", "time": "23:00"}}}).status_code == 201
        response = client.get("/v1/operator/week", headers=AUTH, params={"first_day": "2026-10-19"})
        assert response.status_code == 200, response.text
        body = response.json()
        _check(body)
        assert body["default_scene_id"] is None
        assert client.get("/v1/operator/week", headers=AUTH, params={"first_day": "19 Oct"}).status_code == 422
        assert client.get("/v1/operator/week", params={"first_day": "2026-10-19"}).status_code == 401
