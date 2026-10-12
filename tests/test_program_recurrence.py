"""Programs that repeat (Power and the week, A3): a Schedule's windows come from its When on the
house clock (Europe/London here), worked out in Central at each advance, through the stored
Runtime (PostgreSQL) on a test clock. Weekdays only, across midnight, across both daylight-saving
changes, the Default Scene in the gaps and beneath a window, and a save mid-window that keeps the
playing window's end (R10) while the next window follows the new rule."""
from __future__ import annotations

from support.house_time import EVERY_DAY, LONDON, at, daily, set_house

from central.runtime import Contribution, Program, ProgramDraft, Scene
from central.runtime_store import RuntimeStore

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri")
DAYS = ("12", "13", "14", "15", "16", "17", "18")          # Mon 12 Oct 2026 .. Sun 18 Oct


def _store(registry) -> RuntimeStore:
    set_house(registry.db, LONDON)
    store = RuntimeStore(registry.db, registry.clock)
    for scene_id in ("evening", "calm"):
        store.command("set_scene", Scene(scene_id=scene_id, cycle_seconds=600, loop=True, contributions=(
            Contribution(target="frame:hall", source_refs=("library:1",)),)))
    return store


def _create(store, when, now, *, name="Evenings") -> Program:
    return store.command("create_program", Program(program_id="evenings", name=name, scene_id="evening",
                                                   when=when), now)


def _roots(view, scene_id="evening"):
    return [run for run in view.runs if run.parent_id is None and run.scene_id == scene_id and run.ended_at is None]


def test_a_weekday_schedule_plays_17_to_23_on_weekdays_only(registry):
    store = _store(registry)
    _create(store, daily("17:00", "23:00", WEEKDAYS), at(LONDON, "2026-10-12T12:00"))
    for index, day in enumerate(DAYS):
        evening = store.command("advance", at(LONDON, f"2026-10-{day}T17:30"))
        playing = _roots(evening)
        if index < 5:
            (run,) = playing
            assert (run.started_at, run.program_id) == (at(LONDON, f"2026-10-{day}T17:00"), "evenings")
            assert run.activation_id == f"program:evenings:1:{at(LONDON, f'2026-10-{day}T17:00'):.17g}"
            night = store.command("advance", at(LONDON, f"2026-10-{day}T23:05"))
            assert next(r for r in night.runs if r.run_id == run.run_id).ended_at == at(LONDON, f"2026-10-{day}T23:00")
        else:
            assert playing == []


def test_a_window_across_midnight_ends_the_next_day(registry):
    store = _store(registry)
    _create(store, daily("22:00", "02:00"), at(LONDON, "2026-10-12T12:00"))
    (run,) = _roots(store.command("advance", at(LONDON, "2026-10-12T23:30")))
    assert run.started_at == at(LONDON, "2026-10-12T22:00")
    assert _roots(store.command("advance", at(LONDON, "2026-10-13T01:59")))[0].run_id == run.run_id
    after = store.command("advance", at(LONDON, "2026-10-13T02:05"))
    assert next(r for r in after.runs if r.run_id == run.run_id).ended_at == at(LONDON, "2026-10-13T02:00")


def test_a_window_across_both_daylight_saving_changes(registry):
    """01:30 on the autumn change (2026-10-25) is its first occurrence, in BST (00:30 UTC); on the
    spring change (2027-03-28) 01:30 does not exist and the window starts at 02:30 BST (01:30 UTC)."""
    store = _store(registry)
    _create(store, daily("01:30", "03:00"), at(LONDON, "2026-10-24T12:00"))
    (autumn,) = _roots(store.command("advance", at(LONDON, "2026-10-25T02:30")))
    assert autumn.started_at == 1792888200.0                   # 2026-10-25T00:30:00Z
    store.command("advance", at(LONDON, "2027-03-27T12:00"))
    (spring,) = _roots(store.command("advance", 1806197400.0))  # 2027-03-28T01:30:00Z = 02:30 BST
    assert spring.started_at == 1806197400.0


def test_the_default_scene_plays_in_the_gaps_and_beneath_a_window(registry):
    store = _store(registry)
    store.command("set_default_scene", "calm", at(LONDON, "2026-10-12T12:00"))
    _create(store, daily("17:00", "23:00"), at(LONDON, "2026-10-12T12:00"))
    assert store.command("advance", at(LONDON, "2026-10-12T16:00")).for_target("frame:hall").scene_id == "calm"
    evening = store.command("advance", at(LONDON, "2026-10-12T17:30"))
    assert evening.for_target("frame:hall").scene_id == "evening"
    assert _roots(evening, "calm")                         # still playing beneath
    assert store.command("advance", at(LONDON, "2026-10-12T23:30")).for_target("frame:hall").scene_id == "calm"


def test_a_save_mid_window_keeps_its_end_and_the_next_window_follows_the_new_rule(registry):
    """Mutation probe (P-from): set `program_from = now` on save (drop the playing Run's end) -> the
    new rule's 18:30 window is admitted beside the playing one."""
    store = _store(registry)
    created = _create(store, daily("17:00", "23:00", EVERY_DAY), at(LONDON, "2026-10-12T12:00"))
    (playing,) = _roots(store.command("advance", at(LONDON, "2026-10-12T18:00")))
    saved = store.command("save_program", created, ProgramDraft(
        name="Evenings", scene_id="evening", when=daily("18:30", "22:00")), at(LONDON, "2026-10-12T18:10"))
    assert saved.revision == 2
    (still,) = _roots(store.command("advance", at(LONDON, "2026-10-12T18:45")))
    assert still.run_id == playing.run_id
    assert store.read().export_state()["runs"][playing.run_id]["program_ends_at"] == at(LONDON, "2026-10-12T23:00")
    (tuesday,) = _roots(store.command("advance", at(LONDON, "2026-10-13T18:35")))
    assert tuesday.started_at == at(LONDON, "2026-10-13T18:30")
    assert store.read().export_state()["runs"][tuesday.run_id]["program_ends_at"] == at(LONDON, "2026-10-13T22:00")


def test_save_and_apply_now_finishes_the_playing_window_and_the_new_rule_plays_at_once(registry):
    """Roadmap 2b, Save and apply now: mid-window, the playing Run finishes gracefully now and the new
    rule's window covering now plays at once, to that window's end, under the new rule's identity;
    the next day follows the new rule. (A plain save keeps the playing window: the test above.)

    Mutation probe (P-apply-later): leave the covering window to the next admission
    (`program_from = window.end`, no admission now) -> nothing new plays now."""
    store = _store(registry)
    created = _create(store, daily("17:00", "23:00", EVERY_DAY), at(LONDON, "2026-10-12T12:00"))
    (playing,) = _roots(store.command("advance", at(LONDON, "2026-10-12T18:00")))
    now = at(LONDON, "2026-10-12T18:10")
    saved = store.command("save_program_and_apply_now", created, ProgramDraft(
        name="Evenings", scene_id="calm", when=daily("18:00", "22:00")), now)
    assert saved.revision == 2
    view = store.command("advance", now)
    finishing = next(run for run in view.runs if run.run_id == playing.run_id)
    assert finishing.finish_requested_at == now                 # gracefully: its cycle plays out
    (applied,) = _roots(view, "calm")
    assert (applied.started_at, applied.program_id) == (now, "evenings")
    assert applied.activation_id == saved.window_id(at(LONDON, "2026-10-12T18:00"))
    assert view.for_target("frame:hall").scene_id == "calm"
    state = store.read().export_state()
    assert state["runs"][applied.run_id]["program_ends_at"] == at(LONDON, "2026-10-12T22:00")
    ended = store.command("advance", at(LONDON, "2026-10-12T22:05"))
    assert next(run for run in ended.runs if run.run_id == applied.run_id).ended_at == at(LONDON, "2026-10-12T22:00")
    (tuesday,) = _roots(store.command("advance", at(LONDON, "2026-10-13T18:05")), "calm")
    assert tuesday.started_at == at(LONDON, "2026-10-13T18:00")
    # The same save again is an idempotent retry: nothing changes.
    assert store.command("save_program_and_apply_now", created, ProgramDraft(
        name="Evenings", scene_id="calm", when=daily("18:00", "22:00")), at(LONDON, "2026-10-13T18:06")) == saved
    (still,) = _roots(store.command("advance", at(LONDON, "2026-10-13T18:07")), "calm")
    assert still.run_id == tuesday.run_id


def test_save_and_apply_now_through_the_route_answers_as_put(registry):
    from fastapi.testclient import TestClient
    from test_operator_frames import ADMIN, AUTH

    from central.app import create_app
    set_house(registry.db, LONDON)
    registry.clock.advance(at(LONDON, "2026-10-12T18:00") - registry.clock.utc())
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    store = app.state.coordinator.runtime
    for scene_id in ("evening", "calm"):
        store.command("set_scene", Scene(scene_id=scene_id, cycle_seconds=600, loop=True, contributions=(
            Contribution(target="frame:hall", source_refs=("library:1",)),)))
    with TestClient(app) as client:
        when = {"days": list(EVERY_DAY), "start": {"at": "clock", "time": "17:00"}, "end": {"at": "clock", "time": "23:00"}}
        created = client.post("/v1/operator/programs", headers=AUTH,
                              json={"name": "Evenings", "scene_id": "evening", "when": when}).json()
        draft = {"name": "Evenings", "scene_id": "calm", "when": when}
        applied = client.post(f"/v1/operator/programs/{created['program_id']}/apply-now", headers=AUTH,
                              json={"expected": created, "next": draft})
        assert (applied.status_code, applied.json()["revision"]) == (200, 2), applied.text
        assert store.read().advance(registry.clock.utc()).for_target("frame:hall").scene_id == "calm"
        stale = client.post(f"/v1/operator/programs/{created['program_id']}/apply-now", headers=AUTH,
                            json={"expected": created, "next": {**draft, "name": "Late"}})
        assert (stale.status_code, stale.json()["error"]) == (409, "changed")
        assert stale.json()["current"] == applied.json()
        assert client.post(f"/v1/operator/programs/{created['program_id']}/apply-now",
                           json={"expected": created, "next": draft}).status_code == 401
