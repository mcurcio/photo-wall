"""A Frame's status over PostgreSQL (decision 0036; central/infra/frame_status_store.py): the
scheduler's `status` step keeps since when each Frame's word began, in `frame_status` (migration
132), and Home reads it.

A tick that changes nothing writes nothing; a changed word writes one row and one record in
Central's event log; `since` survives a Central restart; Home's read never writes.

The facts are recorded as Central records them: the Player line's Frame reports and the health
line's verdict through the real link store, the Player's contact through its own store.

Mutation probes: make `record` keep every Frame on every tick (drop the key comparison) -> the
unchanged tick writes a row and an event; stamp `since` with the tick's time on every write of an
unchanged key -> `since` moves after the restart.
"""
from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from support.devices import seed_device
from support.photo_pools import fixture_connection, library_of, seed_source
from support.position import save_position
from test_registry import ADMIN, enroll, frame, hello

from central.app import create_app
from central.content_catalog.catalog import device_id_for_serial
from central.coordination import Coordinator
from central.db import DatabaseTransactionClock, ProcessTransactionClock
from central.fleet.pi_reboot import REBOOT_ACTION, REBOOT_ASKED_ACTION
from central.frame_status.model import (
    CheckNow,
    OverrideKind,
    Part,
    ProblemCode,
    ProgressStep,
    Row,
    SetupStep,
    StatusWord,
)
from central.frame_status.policy import frame_status, house_status
from central.frame_status.ports import FailingStep, Hub, SourceTrouble, facts_of
from central.frame_status.views import FrameStatusView, HomeView
from central.home_routes import FRAME_STATUS, HOME, REBOOTS
from central.infra.events import link_action_in
from central.infra.frame_status_store import PgFrameStatus, PgStatusReading, UnknownFrame
from central.infra.health_report_store import HEALTH_REPORT_SUBJECT, HEALTH_STATE_STREAM
from central.infra.house_store import seed_house
from central.infra.node_link_store import PgLinkStores
from central.infra.player_contact_store import closed_in, opened_in
from central.infra.power_store import PgPower
from central.photo_library.model import ContactRefused, SourceQuery, Unreachable
from central.runtime import Contribution, Scene
from central.runtime_store import RuntimeStore
from contracts.liveness import SILENT_AFTER_SECONDS
from contracts.node_faults import catalogue_digest
from contracts.node_health import HealthReport, OutputHealth, encode_health_report
from contracts.node_link import Pipe
from contracts.node_observation import HostMetricsState, HostMetricV2, encode_host_metrics_state
from contracts.node_output import Power
from contracts.node_state import (
    FRAMES_KEY,
    HOST_LINE,
    HOST_METRICS_KEY,
    PLAYER_LINE,
    FrameReport,
    FrameReports,
    encode_frame_reports,
)
from nodeapi.buffers import state_prefix, state_stream
from nodeapi.epoch import Token
from nodeapi.pull import Batch, Read

pytestmark = pytest.mark.db

SERIAL = "10000000d4a3e136"
SINCE = "2026-11-22T08:10:00Z"
FRAMES_STREAM = state_stream(PLAYER_LINE)
FRAMES_SUBJECT = state_prefix(PLAYER_LINE) + FRAMES_KEY


def _commit(registry, stream: str, subject: str, value: bytes, seq: int) -> None:
    """One state record, committed by the real link store as Central's NodeLink does."""
    read = Read(Token("epoch", seq), subject, {}, value)
    asyncio.run(PgLinkStores(registry.db).store(SERIAL, Pipe.FLEET).commit(
        stream, Batch((read,), Token("epoch", seq), False)))


def _reports(registry, seq: int, *, showing: bool, generation: int = 1) -> None:
    report = FrameReport(frame_id="hall", generation=generation, output_id="HDMI-A-1", showing=showing, light_lux=None)
    _commit(registry, FRAMES_STREAM, FRAMES_SUBJECT, encode_frame_reports(FrameReports(frames=(report,))), seq)


def _status(registry, failing_steps=lambda: ()) -> PgFrameStatus:
    """The store as central/app.py wires it (a new one is a Central restart)."""
    return PgFrameStatus(registry.db, registry.clock, times=DatabaseTransactionClock(),
                         inventory=registry.inventory_in, plans=Coordinator.frame_plans_in,
                         runtime=RuntimeStore.read_in, failing_steps=failing_steps)


def _wall(registry) -> dict:
    """One Pi with the Frame "hall" bound, positioned and heard from, and an unbound Frame "spare"."""
    device = device_id_for_serial(SERIAL)
    seed_house(registry.db, DatabaseTransactionClock())
    with registry.db.transaction() as conn:
        seed_device(conn, device, SERIAL)
    player = enroll(registry, device_id=device)[0]
    hello(registry, player)
    frame(registry, "hall")
    frame(registry, "spare")
    registry.bind("hall", player["player_id"], "HDMI-A-1", expected_generation=0, by="console")
    save_position(registry, "hall")
    with registry.db.transaction() as conn:
        opened_in(conn, player["player_id"], player["authority_epoch"], registry.clock.utc())
    return player


def _plays(registry) -> None:
    """A Scene that targets "hall" plays now: a picture is to play on it (the Runtime's own fact)."""
    runtime = Coordinator(registry.db, registry.clock).runtime
    runtime.command("set_scene", Scene(scene_id="evenings", loop=True, cycle_seconds=60, contributions=(
        Contribution(target="frame:hall", kind="media", source_refs=("library:1",)),)), by="console")
    runtime.command("activate", "evenings", "now-1", registry.clock.utc(), by="console")


def _rows(registry) -> dict[str, dict]:
    with registry.db.transaction() as conn:
        return {row["frame_id"]: dict(row) for row in conn.execute("SELECT * FROM frame_status").fetchall()}


def _events(registry) -> list[dict]:
    with registry.db.transaction() as conn:
        return [row["body"] for row in conn.execute(
            "SELECT body FROM events WHERE kind='frame-status' ORDER BY id").fetchall()]


def _frame(home: HomeView, frame_id: str):
    return next(place.status for wall in home.walls for place in wall.frames if place.frame_id == frame_id)


def test_a_tick_that_changes_nothing_writes_nothing(registry):
    _wall(registry)
    _reports(registry, 1, showing=True)
    status = _status(registry)
    assert status.record() == 2                      # the first tick keeps both Frames
    rows, events = _rows(registry), _events(registry)
    assert (rows["hall"]["word"], rows["hall"]["because"], rows["hall"]["problem"]) == ("showing", "showing", None)
    assert (rows["spare"]["word"], rows["spare"]["setup"], rows["spare"]["because"]) == (None, "needs-a-pi", "setup")
    assert rows["hall"]["since"] == rows["spare"]["since"] == 1000
    assert {row["created_by"] for row in rows.values()} == {"scheduler"} and len(events) == 2
    registry.clock.advance(30)
    _reports(registry, 2, showing=True)              # the Player says the same again
    assert status.record() == 0
    assert (_rows(registry), _events(registry)) == (rows, events)
    status.home()                                    # a read never writes
    assert (_rows(registry), _events(registry)) == (rows, events)


def test_a_changed_word_writes_one_row_and_one_event(registry):
    _wall(registry)
    _plays(registry)
    _reports(registry, 1, showing=True)
    status = _status(registry)
    status.record()
    before = _rows(registry)
    registry.clock.advance(40)
    _reports(registry, 2, showing=False)
    assert status.record() == 1
    rows, events = _rows(registry), _events(registry)
    assert rows["spare"] == before["spare"]
    assert (rows["hall"]["word"], rows["hall"]["because"], rows["hall"]["problem"], rows["hall"]["since"]) == (
        "not-showing", "not-on-screen", "picture-not-on-screen", 1040)
    assert rows["hall"]["created_at"] == before["hall"]["created_at"] and rows["hall"]["changed_at"] >= before["hall"]["changed_at"]
    assert len(events) == 3 and events[-1] == {
        "frame_id": "hall", "word": "not-showing", "setup": None, "row": "not-on-screen",
        "problem": "picture-not-on-screen", "since": 1040}
    # The same word for another problem is a change too: the Pi's verdict now says why.
    verdict = HealthReport(catalogue_digest(), (OutputHealth("HDMI-A-1", True, "app_absent", SINCE),))
    _commit(registry, HEALTH_STATE_STREAM, HEALTH_REPORT_SUBJECT, encode_health_report(verdict), 1)
    registry.clock.advance(5)
    assert status.record() == 1
    assert (_rows(registry)["hall"]["problem"], _rows(registry)["hall"]["since"], len(_events(registry))) == (
        "app-absent", 1045, 4)
    # The Frame page reads its recent changes from the event log, newest first.
    recent = status.frame("hall").recent
    assert [(change.word, change.problem, change.since) for change in recent] == [
        (StatusWord.NOT_SHOWING, ProblemCode.APP_ABSENT, 1045),
        (StatusWord.NOT_SHOWING, ProblemCode.PICTURE_NOT_ON_SCREEN, 1040), (StatusWord.SHOWING, None, 1000)]
    with pytest.raises(UnknownFrame):
        status.frame("no-such-frame")


def test_a_black_frame_no_scene_targets_is_resting_and_one_a_scene_targets_is_not_showing(registry):
    """Whether a picture is to play on a Frame is the Runtime's fact, read with the rest: a Frame no
    Scene targets is black on purpose (its Player states no cause), so it is Resting and the house
    is All good; the same report once a Scene targets it is Not showing. A Scene that blacks the
    Frame wants no picture either."""
    _wall(registry)
    _reports(registry, 1, showing=False)
    status = _status(registry)
    home = status.home()
    hall = _frame(home, "hall")
    assert (hall.word, hall.because.row, hall.problem) == (StatusWord.RESTING, Row.NOTHING_WANTED, None)
    assert (home.house.word, home.house.problems) == ("all-good", ())
    status.record()
    assert (_rows(registry)["hall"]["word"], _rows(registry)["hall"]["because"]) == ("resting", "nothing-wanted")
    runtime = Coordinator(registry.db, registry.clock).runtime
    runtime.command("set_scene", Scene(scene_id="dark", loop=True, cycle_seconds=60, contributions=(
        Contribution(target="frame:hall", kind="black"),)), by="console")
    runtime.command("activate", "dark", "now-0", registry.clock.utc(), by="console")
    assert _frame(status.home(), "hall").because.row is Row.NOTHING_WANTED
    registry.clock.advance(1)
    _plays(registry)                                 # admitted later, so it wins the Frame
    wanted = _frame(status.home(), "hall")
    assert (wanted.word, wanted.because.row, wanted.problem.code) == (
        StatusWord.NOT_SHOWING, Row.NOT_ON_SCREEN, ProblemCode.PICTURE_NOT_ON_SCREEN)


def test_since_survives_a_central_restart(registry):
    _wall(registry)
    _reports(registry, 1, showing=True)
    _status(registry).record()
    registry.clock.advance(7200)
    restarted = _status(registry)                    # nothing of the first is kept in memory
    found = _frame(restarted.home(), "hall")
    assert (found.word, found.since) == (StatusWord.SHOWING, 1000)
    assert restarted.record() == 0 and _rows(registry)["hall"]["since"] == 1000
    assert len(_events(registry)) == 2


def test_home_composes_between_ticks_and_the_row_follows_at_the_next(registry):
    """Home's word is worked out on read: before the step has kept a change, its `since` is the
    read's own time or the fact's; once kept, the row's."""
    player = _wall(registry)
    _reports(registry, 1, showing=True)
    status = _status(registry)
    status.record()
    registry.clock.advance(10)
    with registry.db.transaction() as conn:
        closed_in(conn, player["player_id"], player["authority_epoch"], registry.clock.utc())
    registry.clock.advance(SILENT_AFTER_SECONDS + 1)
    found = _frame(status.home(), "hall")
    assert (found.word, found.because.row, found.since) == (StatusWord.CANT_TELL, Row.PI_NOT_HEARD, 1010)
    assert (found.problem.part, found.problem.code, found.problem.since) == (Part.PI, ProblemCode.PI_NOT_HEARD, 1010)
    assert _rows(registry)["hall"]["word"] == "showing"
    assert status.record() == 1
    assert (_rows(registry)["hall"]["word"], _rows(registry)["hall"]["since"]) == ("cant-tell", 1010)
    assert _frame(status.home(), "hall").since == 1010


def test_home_is_one_document_with_a_change_number_and_a_conditional_read(registry):
    _wall(registry)
    _plays(registry)
    _reports(registry, 1, showing=True)
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    assert REBOOTS in {route.path for route in app.routes}      # the reboot `can` offers is asked at a real route
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + ADMIN}
        assert client.get(HOME).status_code in (401, 403)
        first = client.get(HOME, headers=headers)
        home = HomeView.model_validate(first.json())
        assert first.status_code == 200 and first.headers["etag"] == f'"{home.change}"'
        assert home.house.word == "all-good" and home.house.problems == ()
        assert [each.kind for each in home.overrides] == [OverrideKind.TAKEOVER]      # the Scene shown now
        (wall,) = home.walls
        assert [place.frame_id for place in wall.frames] == ["hall", "spare"]
        hall, spare = (place.status for place in wall.frames)
        assert (hall.word, hall.because.row, hall.problem) == (StatusWord.SHOWING, Row.SHOWING, None)
        assert (spare.word, spare.setup, spare.can) == (None, SetupStep.NEEDS_A_PI, ())
        assert wall.frames[0].device_id == device_id_for_serial(SERIAL) and wall.frames[1].device_id is None
        assert [each.surface_id for each in home.now_next] == [wall.surface_id]
        # Unchanged: no body. The tag names the document, so a stale one is answered in full.
        again = client.get(HOME, headers={**headers, "If-None-Match": first.headers["etag"]})
        assert (again.status_code, again.content, again.headers["etag"]) == (304, b"", first.headers["etag"])
        assert client.get(HOME, headers={**headers, "If-None-Match": '"0"'}).status_code == 200
        _reports(registry, 2, showing=False)
        changed = client.get(HOME, headers={**headers, "If-None-Match": first.headers["etag"]})
        after = HomeView.model_validate(changed.json())
        assert changed.status_code == 200 and after.change != home.change
        assert after.house.word == StatusWord.NOT_SHOWING
        assert [(problem.frame_id, problem.part, problem.code) for problem in after.house.problems] == [
            ("hall", Part.PHOTO_APP, ProblemCode.PICTURE_NOT_ON_SCREEN)]
        page = client.get(FRAME_STATUS.format(frame_id="hall"), headers=headers)
        assert FrameStatusView.model_validate(page.json()).status.word is StatusWord.NOT_SHOWING
        missing = client.get(FRAME_STATUS.format(frame_id="no-such-frame"), headers=headers)
        assert (missing.status_code, missing.json()) == (404, {"error": "unknown_frame"})


def test_a_failing_scheduler_step_is_a_house_problem_home_serves_itself(registry):
    _wall(registry)
    _reports(registry, 1, showing=True)
    home = _status(registry, lambda: (FailingStep("status", 990.0),)).home()
    assert home.house.word == StatusWord.NEEDS_A_LOOK
    assert [(problem.frame_id, problem.code, problem.since, problem.evidence.detail) for problem in home.house.problems] == [
        (None, ProblemCode.SCHEDULER_FAILING, 990, "status")]


def test_a_deleted_frames_row_goes_with_it(registry):
    _wall(registry)
    _status(registry).record()
    with registry.db.transaction() as conn:
        conn.execute("DELETE FROM frames WHERE id='spare'")
    assert set(_rows(registry)) == {"hall"}


def test_the_reading_asks_each_owner_for_its_own_fact(registry):
    """One reading through real owners: Show now is a takeover on its Frame and names the Scene
    playing; a Scene whose Photo source does not exist leaves the Frame's plan unfilled and a
    planning issue on it; All off is a power hold on every Frame; a reboot the host accepted is on
    the way; the host's own throttled flag is its Pi's health."""
    _wall(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", Scene(scene_id="evenings", loop=True, cycle_seconds=60, contributions=(
        Contribution(target="frame:hall", kind="media", source_refs=("library:1",)),)), by="console")
    coordinator.runtime.command("activate", "evenings", "now-1", registry.clock.utc(), by="console")
    assert coordinator.advance().failures == ()
    hold_id = uuid4()
    PgPower(registry.db, times=DatabaseTransactionClock()).all_off(hold_id, by="console")
    device = device_id_for_serial(SERIAL)
    with registry.db.transaction() as conn:      # the ask and its outcome, as central/fleet/pi_reboot.py records them
        link_action_in(conn, device, Pipe.FLEET, REBOOT_ASKED_ACTION, {"request_id": "r-1", "reason": "test", "by": "console"})
        link_action_in(conn, device, Pipe.FLEET, REBOOT_ACTION,
                       {"request_id": "r-1", "reason": "test", "by": "console", "delivered": True})
    throttled = HostMetricsState((HostMetricV2("throttled_now", 1, "boolean"), HostMetricV2("soc_temperature", 91, "celsius")))
    _commit(registry, state_stream(HOST_LINE), state_prefix(HOST_LINE) + HOST_METRICS_KEY,
            encode_host_metrics_state(throttled), 1)
    with registry.db.read_snapshot() as conn:
        reading = PgStatusReading(conn, registry.clock.utc(), times=DatabaseTransactionClock(), inventory=registry.inventory_in,
                                  plans=Coordinator.frame_plans_in, runtime=RuntimeStore.read_in, failing_steps=lambda: ())
        assert reading.pis() == {"hall": device}
        assert reading.playing_scenes() == {"hall": "evenings"}
        assert [issue.code for issue in reading.planning()["hall"]] == ["no_eligible_candidates", "source_missing"]
        assert set(reading.unfilled()) == {"hall"} and reading.preparation()["hall"].planned == 0
        takeover = next(each for each in reading.overrides() if each.kind is OverrideKind.TAKEOVER)
        assert (takeover.frame_ids, takeover.name, takeover.until) == (("hall",), "evenings", None)
        hold = next(each for each in reading.overrides() if each.kind is OverrideKind.POWER_HOLD)
        assert (hold.id, hold.frame_ids, hold.power) == (str(hold_id), ("hall", "spare"), Power.OFF)
        power = reading.power()["hall"]
        assert (power.wanted, power.because, power.resting) == (Power.OFF, "hold", False)   # the Pi has not answered
        (on_the_way,) = reading.on_the_way().values()
        assert set(reading.on_the_way()) == {device} and on_the_way.install is None and on_the_way.host_born_at is None
        assert on_the_way.reboot.delivered is True and on_the_way.reboot.asked_at <= on_the_way.reboot.answered_at
        health = reading.pi_health()[device]
        assert (health.throttled, health.clock_in_doubt) == (True, False)
        assert reading.hub() == Hub("no_hub") and reading.libraries() == ((), 0)
        frames, _ = facts_of(reading)
        assert frames["hall"].override == hold and frames["hall"].scene_id == "evenings"
    home = _status(registry).home()
    hall = _frame(home, "hall")
    assert (hall.word, hall.because.row, hall.progress.step) == (
        StatusWord.GETTING_READY, Row.ON_THE_WAY, ProgressStep.REBOOTING)
    assert [each.kind for each in home.overrides] == [OverrideKind.POWER_HOLD, OverrideKind.TAKEOVER]
    (now_next,) = home.now_next
    assert (now_next.now.kind, now_next.now.scene_id, now_next.next) == ("show-now", "evenings", None)


def test_a_playing_scenes_connection_that_needs_a_look_is_its_frames_and_alone_the_houses(registry):
    """The photo library's own health, for the connection the Frame's playing Scene reads: Needs a
    look on the Frame, with Check now; when no connection answers, one house problem instead."""
    _wall(registry)
    _reports(registry, 1, showing=True)
    library, times = library_of(registry), ProcessTransactionClock(registry.clock)
    connection = fixture_connection(registry)
    with registry.db.transaction() as conn:
        seed_source(conn, "library", SourceQuery(connection_ref=connection))
    assert library.record_contact(library.begin_contact(connection), ContactRefused(problem=Unreachable()))
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", Scene(scene_id="evenings", loop=True, cycle_seconds=60, contributions=(
        Contribution(target="frame:hall", kind="media", source_refs=("library:1",)),)), by="console")
    coordinator.runtime.command("activate", "evenings", "now-1", registry.clock.utc(), by="console")
    registry.clock.advance(20)
    with registry.db.read_snapshot() as conn:
        reading = PgStatusReading(conn, registry.clock.utc(), times=times, inventory=registry.inventory_in,
                                  plans=Coordinator.frame_plans_in, runtime=RuntimeStore.read_in, failing_steps=lambda: ())
        trouble = SourceTrouble(connection, "unreachable", since=1000, checked_at=1000)
        assert reading.sources() == {"hall": (trouble,)} and reading.libraries() == ((trouble,), 1)
        frames, house = facts_of(reading)
        hall = frame_status(frames["hall"], house, reading.now)
        assert (hall.word, hall.problem.part, hall.problem.code, hall.problem.fix, hall.since) == (
            StatusWord.NEEDS_A_LOOK, Part.IMMICH, ProblemCode.LIBRARY_NEEDS_A_LOOK, CheckNow(connection_ref=connection), 1000)
        (problem,) = house_status((hall,), house, reading.now).problems
        assert (problem.frame_id, problem.code, problem.explains) == (None, ProblemCode.NO_LIBRARY_ANSWERS, ("hall",))
