"""The Schedule flow's shape, as a pure module (flow design §7 J6; Power and the week): its steps
and problem routing, its one instance and route, its seed (the Scene prefilled from the shell's
`recentScene` while stored, every day, Normal priority), the edit seed (the stored Program kept
whole as `expected`), the problems a draft has before it is sent, Central's refusal codes by
field, and the body it sends: a weekly When of clock times, never an instant.

Runs the modules under Node, as tests/test_console_flow.py does (same skip and fail rule).
The browser half is tests/browser/test_schedule_flow_browser.py.
"""

import json
import os
import subprocess

from test_console_flow import SRC, _require_node

SCRIPT = r"""
const model = await import(process.argv[1]);
const steps = await import(process.argv[2]);
const out = {};

out.steps = model.SCHEDULE_STEPS.map((s) => s.id);
out.fieldSteps = ["scene", "days", "start", "end", "priority", "name"]
  .map((field) => steps.stepOfField(model.SCHEDULE_FIELD_STEP, field));
out.advanced = [...model.SCHEDULE_ADVANCED_FIELDS].sort();
const K = model.SCHEDULE_KEYS;
out.keys = [
  K.fromRoute({ section: "schedule", flow: "new", step: "when" }),
  K.fromRoute({ section: "schedule", id: "x", flow: "edit", step: "review" }),
  K.fromRoute({ section: "schedule" }),
];
out.editRoute = K.toRoute("edit/x", K.firstStep("edit/x"));
out.route = K.toRoute("new", K.firstStep("new"));
const stored = { evening: { scene_id: "evening" } };
const program = { program_id: "p-1", revision: 3, name: "Evenings", scene_id: "evening", priority: "special-event",
                  when: { days: ["mon", "tue", "wed", "thu", "fri"], start: { at: "clock", time: "17:00" },
                          end: { at: "clock", time: "23:00" } } };
out.editSeed = model.seedSchedule(null, stored, { "p-1": program })("edit/p-1");
out.sunEdit = model.programEditDraft({ ...program, when: { ...program.when, start: { at: "sunset", offset_minutes: 0 } } });
out.seedNone = model.seedSchedule(null, stored)("new");
out.seedRecent = model.seedSchedule("evening", stored)("new");
out.seedGone = model.seedSchedule("ghost", stored)("new");
out.body = model.buildProgram(out.editSeed);
const fields = (draft) => model.programDraftProblems(draft).map((p) => `${p.field}: ${p.message}`);
out.complete = fields(out.editSeed);
out.empty = fields(model.NEW_PROGRAM_DRAFT);
out.noDays = fields({ ...out.editSeed, days: [false, false, false, false, false, false, false] });
out.refusals = model.REFUSAL_FIELDS;
console.log(JSON.stringify(out));
"""


def test_schedule_flow_shape():
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--",
         (SRC / "scheduleFlowModel.js").as_uri(), (SRC / "flow/steps.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True,
        env={**os.environ, "TZ": "Pacific/Auckland"})     # the browser's zone plays no part
    out = json.loads(result.stdout)

    assert out["steps"] == ["scene", "when", "review"]
    assert out["fieldSteps"] == ["scene", "when", "when", "when", "review", "review"]
    assert out["advanced"] == ["priority"]
    assert out["keys"] == ["new", "edit/x", None]
    assert out["editRoute"] == {"section": "schedule", "id": "x", "flow": "edit", "step": "review"}
    assert out["route"] == {"section": "schedule", "flow": "new", "step": "scene"}
    # The stated defaults: every day, Normal; no count, no id (Central names the Program).
    assert out["seedNone"] == {"sceneId": "", "days": [True] * 7, "start": "", "end": "",
                               "priority": "normal", "name": ""}
    assert out["seedRecent"] == {**out["seedNone"], "sceneId": "evening"}
    assert out["seedGone"] == out["seedNone"]
    assert out["editSeed"] == {"sceneId": "evening", "days": [True] * 5 + [False] * 2, "start": "17:00",
                               "end": "23:00", "priority": "special-event", "name": "Evenings",
                               "expected": out["editSeed"]["expected"]}
    assert out["editSeed"]["expected"]["revision"] == 3          # the whole Program, the save's precondition
    assert out["sunEdit"] is None                                 # this editor shows clock times only
    # The body Central stores: the When as clock times on the house clock, never an instant.
    assert out["body"] == {"name": "Evenings", "scene_id": "evening", "priority": "special-event", "when": {
        "days": ["mon", "tue", "wed", "thu", "fri"], "start": {"at": "clock", "time": "17:00"},
        "end": {"at": "clock", "time": "23:00"}}}
    assert out["complete"] == []
    assert out["empty"] == ["scene: Choose a Scene.", "start: Enter when it starts.", "end: Enter when it ends.",
                            "name: Enter a name."]
    assert out["noDays"] == ["days: Tick at least one day."]
    assert {code: route["field"] for code, route in out["refusals"].items()} == {
        "no-days": "days", "unknown-scene": "scene", "needs-place": "start"}
