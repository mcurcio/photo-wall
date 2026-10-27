"""Planning never refuses the wall (fix/planning-without-time-cap), through the real app, its
Coordinator and its Scheduler on PostgreSQL.

The outage of 2026-10-27: a Scene saved with one photo an hour (cycle 3600) made every planning
pass raise ("cycle length exceeds supported preparation extent"), so no Frame got a plan, the
Runtime stopped moving with it, /healthz answered 503 and nothing named the Scene. Each test
here is one of those failures made impossible; each docstring names its mutation probe."""

import pytest
from fastapi.testclient import TestClient
from support.house_time import UTC_HOUSE, set_house, window_program
from support.position import PORTRAIT_ON_WIDE, save_position
from test_operator_session import MARK, ORIGIN, signed_in
from test_registry import ADMIN, enroll, frame

import central.coordination as coordination_module
from central.app import create_app
from central.planner import PlanningError, _ProjectionBuilder
from central.runtime import Contribution, Scene, Transition

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
    put_scene(client, scene("b", "frame-1", 30))
    activate(client, "a")
    run_b = activate(client, "b")
    add = _ProjectionBuilder.add

    def refuse_b(self, intent, window):
        if intent.target == "frame:frame-1":
            raise PlanningError("refused for the test")
        return add(self, intent, window)

    monkeypatch.setattr(_ProjectionBuilder, "add", refuse_b)
    app.state.scheduler.tick()
    plan = offered(app, one)
    # As without B: every 30 s turn from now to the preparation horizon (1320), on frame-0.
    assert [layer.start for layer in plan.layers] == [1000 + 30 * n for n in range(11)]
    assert {layer.frame_id for layer in plan.layers} == {"frame-0"}
    other = offered(app, two)
    assert other is None or other.layers == ()
    assert steps(app)["planning"] == "ok"
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


def test_the_production_shape_plans_after_seventeen_hours(registry, wall):
    """The production Runtime of 2026-10-27, as runtime_state held it: a looping Scene at revision
    8 (one photo an hour, cross-fade 2 s) applied now over its revision-7 Run (30 s) on one Frame,
    and a second good Frame on another Player. Seventeen hours on, one tick: the Runtime and
    planning steps are ok, the hour-long Frame is offered its plan and the good Frame its layers.
    Mutation probe: reinsert the 300 s cycle cap (the planning step fails; no plan)."""
    app, client = wall
    hourly, good = players(registry, 2)
    put_scene(client, scene("test-scene", "frame-0", 30, revision=7))
    put_scene(client, scene("good", "frame-1", 30))
    activate(client, "test-scene")
    activate(client, "good")
    app.state.scheduler.tick()
    eight = scene("test-scene", "frame-0", HOUR, revision=8,
                  transition=Transition(style="cross_fade", seconds=2))
    response = client.post("/v1/operator/scenes/test-scene/apply-now", headers=BEARER,
                           json=eight.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    registry.clock.advance(17 * HOUR)
    app.state.scheduler.tick()
    assert (steps(app)["runtime"], steps(app)["planning"]) == ("ok", "ok")
    now = registry.clock.utc()
    plan = offered(app, hourly)
    assert plan is not None and plan.valid_until > now
    assert any(layer.end - layer.start >= HOUR - 2 and layer.end > now for layer in plan.layers)
    assert offered(app, good).layers
