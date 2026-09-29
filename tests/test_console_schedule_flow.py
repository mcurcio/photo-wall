"""The Schedule flow's shape, as a pure module (flow design §7 J6; bead 4): its steps and
problem routing, its one instance and route, its seed (the Scene prefilled from the shell's
`recentScene` while stored, priority 0, one window), which write a draft makes (one Program, or the
separate-windows helper), and the helper's overlap reason not waiting for the name.

Runs the modules under Node, as tests/test_console_flow.py does (same skip and fail rule).
The browser half is tests/browser/test_schedule_flow_browser.py.
"""

import json
import subprocess

from tests.test_console_flow import SRC, _require_node

SCRIPT = r"""
const model = await import(process.argv[1]);
const steps = await import(process.argv[2]);
const out = {};

out.steps = model.SCHEDULE_STEPS.map((s) => s.id);
out.fieldSteps = ["scene", "start", "end", "weekdays", "count", "priority", "name", "id"]
  .map((field) => steps.stepOfField(model.SCHEDULE_FIELD_STEP, field));
out.advanced = [...model.SCHEDULE_ADVANCED_FIELDS].sort();
const K = model.SCHEDULE_KEYS;
out.keys = [
  K.fromRoute({ section: "schedule", flow: "new", step: "when" }),
  K.fromRoute({ section: "schedule", id: "x", flow: "edit", step: "review" }),
  K.fromRoute({ section: "schedule" }),
];
out.route = K.toRoute("new", K.firstStep("new"));
out.describe = K.describe("new");
const stored = { evening: { scene_id: "evening" } };
out.seedNone = model.seedSchedule(null, stored)("new");
out.seedRecent = model.seedSchedule("evening", stored)("new");
out.seedGone = model.seedSchedule("ghost", stored)("new");
out.separate = [1, "1", " 1 ", 2, "", "abc", 0].map((count) =>
  model.separateWindows({ ...model.NEW_PROGRAM_DRAFT, count }));

const now = new Date("2027-01-01T00:00").getTime() / 1000;
const base = { ...model.NEW_PROGRAM_DRAFT, sceneId: "evening", start: "2027-03-01T18:00",
               end: "2027-03-01T20:00", name: "Show" };
const fields = (draft, ids = new Set(), pending = []) =>
  model.programDraftProblems(draft, ids, now, pending).map((p) => `${p.field}: ${p.message}`);
out.single = fields(base);
out.singleCollision = fields(base, new Set(["show", "show-1"]));
out.singlePending = fields(base, new Set(["show"]), ["show"]);
out.windows = fields({ ...base, count: 3 }, new Set(["show", "show-2"]));
out.windowsPending = fields({ ...base, count: 3 }, new Set(["show-1", "show-2"]), ["show-1", "show-2", "show-3"]);
out.badCount = fields({ ...base, count: "2.5" });
// A 25 h window repeated daily overlaps the next, and says so before a name is given.
out.overlapNoName = fields({ ...base, name: "", end: "2027-03-02T19:00", count: 3 });
console.log(JSON.stringify(out));
"""


def test_schedule_flow_shape():
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--",
         (SRC / "scheduleFlowModel.js").as_uri(), (SRC / "flow/steps.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True)
    out = json.loads(result.stdout)

    assert out["steps"] == ["scene", "when", "review"]
    # The window and its helper on When; the name, its id and the priority on Review.
    assert out["fieldSteps"] == [
        "scene", "when", "when", "when", "when", "review", "review", "review"]
    assert out["advanced"] == ["count", "id", "priority", "weekdays"]
    # One instance, "new"; no edit routes.
    assert out["keys"] == ["new", None, None]
    assert out["route"] == {"section": "schedule", "flow": "new", "step": "scene"}
    assert out["describe"] == "a new Program"
    # The stated defaults: one window, every weekday, priority 0; the Scene prefilled.
    assert out["seedNone"] == {
        "sceneId": "", "start": "", "end": "", "count": 1, "weekdays": [True] * 7,
        "priority": 0, "name": "", "idOverride": None}
    assert out["seedRecent"] == {**out["seedNone"], "sceneId": "evening"}
    # A Scene deleted since it was handed over prefills nothing, as in Show now.
    assert out["seedGone"] == out["seedNone"]
    # Only exactly one window is one Program; anything else is the helper, whose count
    # reason then applies (never silently read as one).
    assert out["separate"] == [False, False, False, True, True, True, True]

    assert out["single"] == []
    # One Program collides on its own id only.
    assert out["singleCollision"] == [
        "name: A Program called show already exists; choose another name."]
    # Its own earlier, unanswered Program is not a collision when it is sent again.
    assert out["singlePending"] == []
    # Separate windows collide on their window ids, never the base id.
    assert out["windows"] == ["name: show-2 already exists."]
    # This draft's own earlier windows are not collisions when it is sent again.
    assert out["windowsPending"] == []
    assert out["badCount"] == ["count: Between 1 and 60 windows."]
    assert out["overlapNoName"] == [
        "name: Enter a name.", "weekdays: Each window must end before the next starts."]
