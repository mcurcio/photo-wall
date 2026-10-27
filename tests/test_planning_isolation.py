"""Planning never refuses the wall (fix/planning-without-time-cap), through the real app, its
Coordinator and its Scheduler on PostgreSQL.

The outage of 2026-10-27: a Scene saved with one photo an hour (cycle 3600) made every planning
pass raise ("cycle length exceeds supported preparation extent"), so no Frame got a plan, the
Runtime stopped moving with it, /healthz answered 503 and nothing named the Scene. Each test
here is one of those failures made impossible; each docstring names its mutation probe."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from support.house_time import UTC_HOUSE, set_house, window_program
from support.position import PORTRAIT_ON_WIDE, save_position
from test_operator_session import MARK, ORIGIN, signed_in
from test_registry import ADMIN, enroll, frame

import central.coordination as coordination_module
from central.app import create_app
from central.infra.planning_outcome_store import PgPlanningOutcome
from central.planner import Diagnostic, PlanningError, Projection, _ProjectionBuilder
from central.registry import RegistryError
from central.runtime import Contribution, Runtime, Scene

pytestmark = pytest.mark.db

BEARER = {"Authorization": "Bearer " + ADMIN}
HOUR = 3600.0


def players(registry, count):
    """`count` Players, each bound to its own Frame frame-<n> (as tests/test_coordination.py)."""
    enrolled = []
    for index in range(count):
        player, _, _ = enroll(registry)
        frame(registry, f"frame-{index}")
        registry.bind(f"frame-{index}", player["player_id"], "HDMI-A-1", expected_generation=0)
        save_position(registry, f"frame-{index}", PORTRAIT_ON_WIDE)
        enrolled.append(player)
    return enrolled


def scene(scene_id, frame_id, cycle, **fields):
    return Scene(scene_id=scene_id, cycle_seconds=cycle, loop=True, contributions=(
        Contribution(target=f"frame:{frame_id}", kind="black"),), **fields)


def put_scene(client, value, headers=BEARER):
    response = client.put(f"/v1/operator/scenes/{value.scene_id}",
                          json=value.model_dump(mode="json"), headers=headers)
    assert response.status_code == 200, response.text
    return response


def activate(client, scene_id):
    response = client.post("/v1/operator/activations", headers=BEARER,
                           json={"scene_id": scene_id, "activation_id": f"go-{scene_id}"})
    assert response.status_code == 200, response.text
    return response.json()["run_id"]


def offered(app, player):
    return app.state.coordinator.delivery(player["player_id"], player["authority_epoch"])["plan"]


def steps(app):
    return {name: step.status for name, step in app.state.scheduler.health().steps.items()}


def unavailable(registry):
    """Each `offer_unavailable` event as (player, code)."""
    with registry.db.transaction() as conn:
        return [(row["player_id"], row["detail"]["code"]) for row in conn.execute(
            "SELECT player_id, detail FROM execution_events WHERE kind='offer_unavailable' "
            "ORDER BY sequence").fetchall()]


def two_offered(registry, client, app):
    """Two Players on frame-0 and frame-1, each with its own 30 s Scene playing."""
    one, two = players(registry, 2)
    put_scene(client, scene("a", "frame-0", 30))
    put_scene(client, scene("b", "frame-1", 30))
    activate(client, "a")
    activate(client, "b")
    return one, two


@pytest.fixture
def wall(registry):
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    with TestClient(app) as client:
        yield app, client


def test_a_saved_hour_long_scene_is_planned(registry, wall):
    """Save implies plan: a Scene of one photo an hour, saved through the operator API, is offered
    to its Player after one tick as one layer over the whole hour, valid to its end, and the
    planning step is ok. Mutation probe: reinsert `if intent.interval_end -
    intent.interval_start > 300: raise PlanningError(...)` in `_ProjectionBuilder.add` (no layer;
    `intent_refused`)."""
    app, client = wall
    (player,) = players(registry, 1)
    put_scene(client, scene("s", "frame-0", HOUR))
    activate(client, "s")
    app.state.scheduler.tick()
    plan = offered(app, player)
    assert [(layer.start, layer.end) for layer in plan.layers] == [(1000, 1000 + HOUR)]
    assert plan.valid_until == 1000 + HOUR
    assert steps(app)["planning"] == "ok"
    assert not client.get("/v1/operator/snapshot", headers=BEARER).json()["planning"]["issues"]


def test_one_refused_scene_leaves_every_other_frame_planned(registry, wall, monkeypatch):
    """Isolation: Scene B, which the Planner refuses, is named (`intent_refused`, its Scene and
    Run) in the operator snapshot, while Scene A's Player is offered its plan and the planning
    step stays ok. Mutation probe: remove the try/except around `add` in `project` (the whole
    pass fails: Player one gets no plan)."""
    app, client = wall
    one, two = players(registry, 2)
    put_scene(client, scene("a", "frame-0", 30))
    activate(client, "a")
    app.state.scheduler.tick()
    baseline = offered(app, one)                     # Scene A alone
    put_scene(client, scene("b", "frame-1", 30))
    run_b = activate(client, "b")
    add = _ProjectionBuilder.add

    def refuse_b(self, intent, window):
        if intent.target == "frame:frame-1":
            raise PlanningError("refused for the test")
        return add(self, intent, window)

    monkeypatch.setattr(_ProjectionBuilder, "add", refuse_b)
    app.state.scheduler.tick()
    plan = offered(app, one)
    assert plan.layers == baseline.layers and baseline.layers
    other = offered(app, two)
    assert other is None or other.layers == ()
    assert steps(app)["planning"] == "ok"
    assert client.get("/healthz").status_code == 200
    issues = client.get("/v1/operator/snapshot", headers=BEARER).json()["planning"]["issues"]
    refused = [issue for issue in issues if issue["code"] == "intent_refused"]
    assert [(issue["scene_id"], issue["run_id"], issue["reason"]) for issue in refused] == [
        ("b", run_b, "refused for the test")]


def test_the_runtime_moves_on_and_commits_settle_when_planning_fails(registry, wall, monkeypatch):
    """The Runtime is its own step: with planning failing, a Program window that opens after the
    last tick still opens (the saved Runtime is at the new time, its Run admitted), and an
    expired pin is still released (settle). Mutation probe: move `runtime.advance` back inside
    `_plan`'s transaction (runtime_state's `now` stays put)."""
    app, client = wall
    players(registry, 1)
    set_house(registry.db, UTC_HOUSE)
    put_scene(client, scene("evening", "frame-0", 30))
    response = client.post("/v1/operator/programs", headers=BEARER,
                           json=window_program("evening", "evening", 1020, 1080).model_dump(
                               mode="json", exclude={"program_id", "revision"}))
    assert response.status_code == 201, response.text
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO media_references(owner,variant_key,expires_at) "
                     "VALUES('expired-offer','asset.recipe',1010)")

    def broken(*_args, **_kwargs):
        raise RuntimeError("planning is broken for the test")

    monkeypatch.setattr(coordination_module, "project", broken)
    registry.clock.advance(25)                                      # 1025: the window is open
    app.state.scheduler.tick()
    assert steps(app)["planning"] == "failing"
    assert (steps(app)["runtime"], steps(app)["settle"]) == ("ok", "ok")
    with registry.db.transaction() as conn:
        state = conn.execute("SELECT snapshot, changed_by FROM runtime_state").fetchone()
        pins = conn.execute("SELECT count(*) AS n FROM media_references").fetchone()["n"]
    assert state["snapshot"]["now"] == 1025 and state["changed_by"] == "scheduler"
    assert [run["scene"]["scene_id"] for run in state["snapshot"]["runs"].values()] == ["evening"]
    assert pins == 0


def test_every_runtime_command_records_who_ran_it(registry, wall):
    """Audit: a Scene saved through the console's session cookie is one runtime_changes row by
    "console", through the bearer token by "api-token", each naming its command, Scene, revision
    and time; the scheduler's tick stamps runtime_state as "scheduler". Mutation probe: hardcode
    `by="console"` in `OperatorAuth.admin` (the bearer row reads "console")."""
    app, client = wall
    players(registry, 1)
    cookie = signed_in(client)
    put_scene(client, scene("s", "frame-0", HOUR), headers={**cookie, **MARK, "Origin": ORIGIN})
    registry.clock.advance(2)
    put_scene(client, scene("s", "frame-0", 60, revision=2))
    with registry.db.transaction() as conn:
        rows = conn.execute("SELECT EXTRACT(EPOCH FROM at)::float8 AS at, by, command, scene_id, "
                            "scene_revision FROM runtime_changes ORDER BY id").fetchall()
        created = conn.execute("SELECT created_by, changed_by FROM runtime_state").fetchone()
    assert [dict(row) for row in rows] == [
        {"at": 1000, "by": "console", "command": "save_scene", "scene_id": "s", "scene_revision": 1},
        {"at": 1002, "by": "api-token", "command": "save_scene", "scene_id": "s", "scene_revision": 2}]
    assert dict(created) == {"created_by": "console", "changed_by": "api-token"}
    app.state.scheduler.tick()
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT changed_by FROM runtime_state").fetchone()["changed_by"] == \
            "scheduler"


PRODUCTION = Path(__file__).parent / "fixtures" / "runtime_state_2026_10_27.json"


def test_the_production_runtime_plans_after_seventeen_hours(registry, wall):
    """The production Runtime of 2026-10-27 as runtime_state held it (operator configuration
    only, its Source renamed): the looping Scene `test-scene` at revision 8 (one photo an hour,
    cross-fade 2 s) applied now over its revision-7 Run (30 s) on `my-frame`, and a second good
    Frame on another Player. Seventeen hours on, one tick through the real Coordinator: the
    Runtime and planning steps are ok, `my-frame` is offered the hour-long layer and the good
    Frame its layers. Mutation probe: reinsert the 300 s cycle cap (the hour is refused: no
    showing of an hour on `my-frame`)."""
    app, client = wall
    snapshot = json.loads(PRODUCTION.read_text())
    registry.clock.advance(snapshot["now"] - registry.clock.utc())
    hourly, _, _ = enroll(registry)
    frame(registry, "my-frame")
    registry.bind("my-frame", hourly["player_id"], "HDMI-A-1", expected_generation=0)
    save_position(registry, "my-frame", PORTRAIT_ON_WIDE)
    good, _, _ = enroll(registry)
    frame(registry, "good")
    registry.bind("good", good["player_id"], "HDMI-A-1", expected_generation=0)
    save_position(registry, "good", PORTRAIT_ON_WIDE)
    runtime = Runtime.restore(snapshot, house=UTC_HOUSE)
    runtime.set_scene(scene("good-scene", "good", 30))
    runtime.activate("good-scene", "go-good", snapshot["now"])
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO runtime_state(singleton,revision,snapshot,created_at,created_by,"
                     "changed_at,changed_by) VALUES(TRUE,1,%(snapshot)s,%(now)s,'console',"
                     "%(now)s,'console') ON CONFLICT(singleton) DO UPDATE SET "
                     "snapshot=EXCLUDED.snapshot",
                     {"snapshot": Jsonb(runtime.export_state()), "now": snapshot["now"]})
    registry.clock.advance(17 * HOUR)
    app.state.scheduler.tick()
    assert (steps(app)["runtime"], steps(app)["planning"]) == ("ok", "ok")
    now = registry.clock.utc()
    plan = offered(app, hourly)
    assert plan is not None and plan.valid_until > now
    # Its Source holds no photo here: the hour is planned as one unfilled showing, not refused.
    projection = app.state.coordinator.advance().projection
    assert any(photo.frame_id == "my-frame" and photo.end - photo.start >= HOUR
               and photo.start <= now < photo.end for photo in projection.plan)
    assert offered(app, good).layers
    issues = client.get("/v1/operator/snapshot", headers=BEARER).json()["planning"]["issues"]
    assert not [issue for issue in issues if issue["code"] in ("intent_refused", "plan_shortened")]


def test_a_player_gone_stale_is_left_out_alone(registry, wall, monkeypatch):
    """The Player is the unit of isolation: one whose authority goes stale between its session's
    read and its configuration is left out of the pass (`offer_unavailable`, stale_authority)
    and the other is offered. Mutation probe: re-raise in `_configurations` (planning fails)."""
    app, client = wall
    one, two = two_offered(registry, client, app)
    installation = app.state.coordinator.installation
    configuration_in = installation.configuration_in

    def stale_two(conn, player_id, epoch):
        return None if player_id == two["player_id"] else configuration_in(conn, player_id, epoch)

    monkeypatch.setattr(installation, "configuration_in", stale_two)
    app.state.scheduler.tick()
    assert steps(app)["planning"] == "ok"
    assert offered(app, one).layers
    assert (two["player_id"], "stale_authority") in unavailable(registry)


@pytest.mark.parametrize("failure", ["registry", "database"])
def test_one_players_offer_that_fails_leaves_the_others_offered(registry, wall, monkeypatch,
                                                                 failure):
    """Any failure while offering one Player, a registry refusal or a statement the database
    rejects, rolls back to that Player's savepoint (`offer_unavailable` naming it) and every
    other Player is offered. Mutation probe: catch only `media_unavailable` in `_propose`, or
    drop its savepoint (the database case aborts the whole pass)."""
    app, client = wall
    one, two = two_offered(registry, client, app)
    coordinator = app.state.coordinator
    offer = coordinator._offer

    def failing(conn, plan, groups):
        if plan.player_id == two["player_id"]:
            if failure == "registry":
                raise RegistryError("other_code", 409)
            conn.execute("SELECT * FROM no_such_table")
        return offer(conn, plan, groups)

    monkeypatch.setattr(coordinator, "_offer", failing)
    app.state.scheduler.tick()
    assert steps(app)["planning"] == "ok"
    assert offered(app, one).layers
    assert offered(app, two) is None
    codes = [code for player, code in unavailable(registry) if player == two["player_id"]]
    assert codes and (codes[0] == "other_code" if failure == "registry"
                      else codes[0].startswith("UndefinedTable at "))


def test_one_players_inconsistent_lock_leaves_the_others_offered(registry, wall):
    """A Player holding a content lock that disagrees with its offer is left out alone
    (`offer_unavailable`, inconsistent_content_lock; its offers stand); the other Player is
    still planned. Mutation probe: raise `CoordinationError("inconsistent_content_lock")` in
    `_locks` again (planning fails)."""
    app, client = wall
    one, two = two_offered(registry, client, app)
    app.state.scheduler.tick()
    held = offered(app, two)
    layer = held.layers[0].model_copy(update={"opacity": 0.5})
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO assignment_locks(player_id,authority_epoch,assignment_id,layer,"
                     "valid_until,secured_at) VALUES(%s,%s,%s,%s,%s,%s)",
                     (two["player_id"], two["authority_epoch"], layer.assignment_id,
                      Jsonb(layer.model_dump(mode="json")), layer.end, 1000))
    response = client.post("/v1/operator/scenes/a/apply-now", headers=BEARER,
                           json=scene("a", "frame-0", 20, revision=2).model_dump(mode="json"))
    assert response.status_code == 200, response.text
    app.state.scheduler.tick()
    assert steps(app)["planning"] == "ok"
    assert 20 in {layer.end - layer.start for layer in offered(app, one).layers}
    assert offered(app, two) == held
    assert (two["player_id"], "inconsistent_content_lock") in unavailable(registry)


def test_a_wall_of_129_players_is_coordinated(registry, wall):
    """No count of Players bounds the Coordinator: 129 Players, each with its Frame, are all
    offered. Mutation probe: re-add the 128-Player `player_limit` to `Coordinator._players`."""
    app, client = wall
    enrolled = players(registry, 129)
    put_scene(client, Scene(scene_id="all", cycle_seconds=30, loop=True, contributions=tuple(
        Contribution(target=f"frame:frame-{n}", kind="black") for n in range(129))))
    activate(client, "all")
    app.state.scheduler.tick()
    assert steps(app)["planning"] == "ok"
    assert all(offered(app, player).layers for player in enrolled)


def test_the_planning_outcome_keeps_since_while_its_issues_stand(registry):
    """`since` is when the current set of issues began: an unchanged pass keeps it, a changed
    set starts it again. Mutation probe: drop the unchanged-issues early return in
    `PgPlanningOutcome.record_in` (the second pass moves `since`)."""
    store = PgPlanningOutcome(registry.clock)

    def projection(*codes):
        return Projection(now=0, horizon_end=1, valid_until=1, players=(), acquisitions=(),
                          diagnostics=tuple(Diagnostic(code=code, scene_id="s") for code in codes),
                          selections=())

    def record(*codes):
        with registry.db.transaction() as conn:
            store.record_in(conn, projection(*codes))
            return store.read_in(conn)

    first = record("intent_refused")
    registry.clock.advance(60)
    assert record("intent_refused").since == first.since == 1000
    changed = record("intent_refused", "plan_shortened")
    assert changed.since == 1060 and len(changed.issues) == 2


def test_each_operator_command_names_what_it_changed(registry, wall):
    """Every Runtime write is audited by what it changed (`RuntimeStore.edit`): cancelling a Run
    names the Run and its Scene, deleting a Scene names the Scene. Mutation probe: drop the
    runtime_changes insert from `edit` (no rows)."""
    app, client = wall
    players(registry, 1)
    put_scene(client, scene("s", "frame-0", 30))
    run = activate(client, "s")
    registry.clock.advance(1)
    response = client.post(f"/v1/operator/runs/{run}/cancel", headers=BEARER)
    assert response.status_code == 200, response.text
    response = client.delete("/v1/operator/scenes/s", headers=BEARER, params={"expected_revision": 1})
    assert response.status_code in (200, 204), response.text
    with registry.db.transaction() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT by, command, scene_id, run_id FROM runtime_changes ORDER BY id").fetchall()]
    assert {"by": "api-token", "command": "activate", "scene_id": "s", "run_id": run} in rows
    assert {"by": "api-token", "command": "cancel", "scene_id": "s", "run_id": run} in rows
    assert {"by": "api-token", "command": "delete_scene", "scene_id": "s", "run_id": None} in rows
