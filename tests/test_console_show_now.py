"""The Show-now flow's pure parts (flow design §6 frozen surface, §7 J7; bead 5):
`coveringPriority` and its helpers in showState.js, and showNowModel.js (steps, keys,
seed and the activation key's rules), run under Node as tests/test_console_flow.py runs
the flow kit. Without Node it skips on a developer machine, but FAILS where the checks
are meant to run in full (`CI` or `PHOTO_WALL_BROWSER_TESTS` set).
"""

import json
import subprocess
from pathlib import Path

from tests.test_console_flow import _require_node

SRC = Path(__file__).parents[1] / "central/console/src"

SCRIPT = r"""
const show = await import(process.argv[1]);
const model = await import(process.argv[2]);
const refresh = await import(process.argv[3]);
const out = {};

// Served Runs in admission order (central/runtime.py `_view`: sorted by `order`).
const run = (run_id, scene_id, priority, frames, extra = {}) => ({
  run_id, scene_id, priority, root_id: run_id, parent_id: null, phase: "body",
  participants: frames.map((frame) => `frame:${frame}`), children: [], ...extra });
const snapshot = { runtime: { current: { runs: [
  run("r-low", "low", 1, ["lobby"]),
  run("r-high", "high", 5, ["hall"]),
  run("r-later", "later", 5, ["lobby", "hall"]),
  run("r-ended", "ended", 9, ["lobby"], { phase: "completed" }),
  run("r-child", "child", 8, ["lobby"], { parent_id: "r-low", root_id: "r-low" }),
  run("r-outro", "outro", 3, ["den"], { phase: "outro" }),
  run("r-negative", "negative", -2, ["attic"]),
] } } };

out.covering = [
  show.coveringPriority(snapshot, ["lobby"]),
  show.coveringPriority(snapshot, ["hall", "lobby"]),
  show.coveringPriority(snapshot, ["den"]),
  show.coveringPriority(snapshot, ["attic"]),
  show.coveringPriority(snapshot, ["garage"]),
  show.coveringPriority(snapshot, []),
  show.coveringPriority(null, ["lobby"]),
];
out.runs = show.coveringRuns(snapshot, ["lobby", "hall"]).map(({ run, frames }) => [run.run_id, frames]);
out.underneath = [
  show.underneathSentence(snapshot, ["lobby", "hall"], 5),
  show.underneathSentence(snapshot, ["lobby", "hall"], 2),
  show.underneathSentence(snapshot, ["lobby"], 0),
];
// A Scene that protects frames is refused below a higher covering Run
// (central/runtime.py `_protected_conflict`: `protection_not_visible`).
out.refused = [
  show.underneathSentence(snapshot, ["lobby", "hall"], 2, ["lobby"]),
  show.underneathSentence(snapshot, ["lobby", "hall"], 2, ["lobby", "hall"]),
  show.underneathSentence(snapshot, ["lobby", "hall"], 5, ["lobby", "hall"]),
  show.underneathSentence(snapshot, ["lobby"], 0, ["attic"]),
];
// A live Run whose Scene protects a frame refuses the activation at ANY priority
// (central/runtime.py `_protected_conflict`: `protected_frames`, no force).
const guarded = { runtime: { ...snapshot.runtime, protected_frames: { "r-high": ["frame:hall"] } } };
out.protecting = {
  runs: show.protectingRuns(guarded, ["lobby", "hall"]).map(({ run, frames }) => [run.run_id, frames]),
  sentences: [
    show.underneathSentence(guarded, ["lobby", "hall"], 9),
    show.underneathSentence(guarded, ["hall"], 0, ["hall"]),
    show.underneathSentence(guarded, ["lobby"], 9),
  ],
};
out.protectedFrames = [
  show.sceneProtectedFrames({ protect_frames: true,
    contributions: [{ target: "frame:b" }, { target: "actuator:x" }],
    outro_contributions: [{ target: "frame:a" }],
    children: [{ scene: { contributions: [{ target: "frame:c" }] } }] }),
  show.sceneProtectedFrames({ contributions: [{ target: "frame:a" }],
    children: [{ scene: { protect_frames: true, contributions: [{ target: "frame:c" }] } }] }),
  show.sceneProtectedFrames({ contributions: [{ target: "frame:a" }] }),
  show.sceneProtectedFrames(undefined),
];
out.sceneFrames = [
  show.sceneFrames({ contributions: [{ target: "frame:b" }, { target: "actuator:x" }],
                     outro_contributions: [{ target: "frame:a" }],
                     children: [{ scene: { contributions: [{ target: "frame:c" }, { target: "frame:b" }] } }] }),
  show.sceneFrames(undefined),
];
out.sceneSourceRefs = [
  show.sceneSourceRefs({
    contributions: [{ target: "frame:b", source_refs: ["z:2", "a:1"] },
                    { target: "actuator:x" }],
    outro_contributions: [{ target: "frame:a", source_refs: ["a:1", "outro:1"] }],
    children: [{ scene: {
      contributions: [{ target: "frame:c", source_refs: ["child:1", "z:2"] }],
      children: [{ scene: { outro_contributions: [
        { target: "frame:d", source_refs: ["nested:1"] }] } }],
    } }],
  }),
  show.sceneSourceRefs({ contributions: [{ target: "frame:a", asset_refs: ["fixed-photo"] }] }),
  show.sceneSourceRefs(undefined),
];
out.authoredMedia = [
  show.sceneHasAuthoredMedia({ contributions: [{ target: "frame:a", asset_refs: ["fixed"] }] }),
  show.sceneHasAuthoredMedia({ children: [{ scene: {
    outro_contributions: [{ target: "frame:a", asset_refs: ["fixed"] }] } }] }),
  show.sceneHasAuthoredMedia({ contributions: [{ target: "frame:a", source_refs: ["live:1"] }] }),
  show.sceneHasAuthoredMedia(undefined),
];
const healthySource = { refresh_completed_revision: 7 };
const healthyState = { state: "ok", label: "refreshed recently" };
out.refreshFeedback = {
  unknown: refresh.sourceRefreshMessage(
    refresh.sourceRefreshResult({ ok: false, status: 503 }), healthySource, healthyState),
  failed: refresh.sourceRefreshMessage(
    refresh.sourceRefreshResult({ ok: false, status: 409, error: "refresh_conflict" }),
    healthySource, healthyState),
  requestedWithoutRevision: refresh.sourceRefreshMessage(
    refresh.sourceRefreshResult({ ok: true, status: 202, data: {} }),
    healthySource, healthyState),
  completed: refresh.sourceRefreshMessage(
    refresh.sourceRefreshResult({ ok: true, status: 202,
      data: { requested_revision: 7 } }), healthySource, healthyState),
};

// The flow's shape.
out.steps = model.SHOW_STEPS.map((step) => step.id);
out.fieldSteps = ["scene", "priority", "repeat"].map((field) => model.SHOW_FIELD_STEP[field]);
out.advanced = [...model.SHOW_ADVANCED_FIELDS].sort();
const K = model.SHOW_KEYS;
out.keys = [
  K.fromRoute({ section: "now", flow: "show", step: "review" }),
  K.fromRoute({ section: "now" }),
  K.fromRoute({ section: "scenes", flow: "new", step: "kind" }),
];
out.route = K.toRoute("new", "scene");
out.first = K.firstStep("new");

// The seed and the activation key.
let minted = 0;
const mint = () => `key-${++minted}`;
const definitions = { evening: { scene_id: "evening" } };
out.seedRecent = model.seedShowNow("evening", definitions, mint)();
out.seedGone = model.seedShowNow("ghost", definitions, mint)();
out.seedNone = model.seedShowNow(null, definitions, mint)();
const value = { sceneId: "evening", priority: 3, repeat: "ignore", activationKey: "kept" };
out.same = model.editActivation({ repeat: "ignore" }, mint)(value);
out.repeat = model.editActivation({ repeat: "restart" }, mint)(value);
out.scene = model.editActivation({ sceneId: "morning" }, mint)(value);
out.priority = model.editActivation({ priority: "4" }, mint)(value);
// How an answer to the activation ends the flow.
const answer = (status, error = null) => model.activationAnswer(
  { ok: status >= 200 && status < 300, status, error, data: null });
out.answers = [
  model.activationAnswer(null),
  answer(503),
  answer(200),
  answer(409, "some_conflict"),
  answer(422, "invalid_command"),
  answer(401, "unauthorized"),
  answer(403, "origin_mismatch"),
  answer(403, "request_unmarked"),
  answer(422, "invalid_request"),
  answer(404),
];
out.shown = [
  model.shownPriority({ ...value, priority: null }, 5),
  model.shownPriority(value, 5),
  model.shownPriority({ ...value, priority: "" }, 5),
];
console.log(JSON.stringify(out));
"""


def test_covering_priority_and_the_show_now_model():
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--",
         (SRC / "showState.js").as_uri(), (SRC / "showNowModel.js").as_uri(),
         (SRC / "useSourceRefresh.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True)
    out = json.loads(result.stdout)

    # The highest priority among the LIVE ROOT Runs covering any of the frames; 0 when
    # none does. An ended Run (9) and a child Run (8) never count; an outro still does;
    # a lone negative priority is the highest there is. Max, not max + 1: at equal
    # priority the later admission is on top (central/runtime.py:174,576,587,794).
    assert out["covering"] == [5, 5, 3, -2, 0, 0, 0]
    # Highest first; at equal priority the later admission first. Each with its frames.
    assert out["runs"] == [
        ["r-later", ["lobby", "hall"]], ["r-high", ["hall"]], ["r-low", ["lobby"]]]
    assert out["underneath"] == [
        None,
        "At priority 2 this stays underneath the Run of later (priority 5) on lobby, hall; "
        "and the Run of high (priority 5) on hall.",
        "At priority 0 this stays underneath the Run of later (priority 5) on lobby; "
        "and the Run of low (priority 1) on lobby.",
    ]
    # A Run protecting one of the frames refuses the activation at any priority, before
    # anything else is said; a Run protecting none of them changes nothing.
    assert out["protecting"] == {
        "runs": [["r-high", ["hall"]]],
        "sentences": [
            "Central will refuse this at any priority: hall is protected by the Run of high.",
            "Central will refuse this at any priority: hall is protected by the Run of high.",
            None]}
    # A protecting Scene below a higher covering Run is refused, not "underneath": each
    # refusing Run is named with the protected frames it covers, and the priority that
    # would be accepted.
    assert out["refused"] == [
        "At priority 2 Central will refuse this: it protects lobby, which the Run of later "
        "(priority 5) covers. Use priority at least 5.",
        "At priority 2 Central will refuse this: it protects lobby, hall, which the Run of "
        "later (priority 5) covers, and hall, which the Run of high (priority 5) covers. "
        "Use priority at least 5.",
        None,
        "At priority 0 this stays underneath the Run of later (priority 5) on lobby; "
        "and the Run of low (priority 1) on lobby.",
    ]
    # The frames a Scene protects (central/runtime.py `Scene.protected_frames`): all of
    # its frames when it protects them, else its children's protected frames.
    assert out["protectedFrames"] == [["a", "b", "c"], ["c"], [], []]
    # A Scene reaches its own, its outro's and its children's frames.
    assert out["sceneFrames"] == [["a", "b", "c"], []]
    assert out["sceneSourceRefs"] == [["a:1", "child:1", "nested:1", "outro:1", "z:2"], [], []]
    assert out["authoredMedia"] == [True, True, False, False]
    assert out["refreshFeedback"] == {
        "unknown": "The refresh request outcome is unknown. Check the Source status before retrying.",
        "failed": "Refresh request failed: refresh conflict.",
        "requestedWithoutRevision": "Refresh requested. The status will update when the media worker finishes.",
        "completed": "Refresh finished. Current Source status: refreshed recently.",
    }

    assert out["steps"] == ["scene", "review"]
    assert out["fieldSteps"] == ["scene", "review", "review"]
    assert out["advanced"] == ["priority", "repeat"]
    assert out["keys"] == ["new", None, None]
    assert out["route"] == {"section": "now", "flow": "show", "step": "scene"}
    assert out["first"] == "scene"

    # The seed: the recent Scene while it is stored, the priority on its default, "Leave
    # it running" and a fresh key.
    assert out["seedRecent"] == {
        "sceneId": "evening", "priority": None, "repeat": "ignore", "activationKey": "key-1"}
    assert out["seedGone"]["sceneId"] == "" and out["seedNone"]["sceneId"] == ""
    # A change to the form mints a new key; the value it already has changes nothing.
    # A new Scene puts the priority back on its default.
    assert out["same"] is None
    assert out["repeat"] == {"repeat": "restart", "activationKey": "key-4"}
    assert out["scene"] == {"priority": None, "sceneId": "morning", "activationKey": "key-5"}
    assert out["priority"] == {"priority": "4", "activationKey": "key-6"}
    assert out["shown"] == [5, 3, ""]

    # An answer ends the flow only when Central's Runtime gave it (an Admission, or a
    # refusal of the command); one refused before the Runtime (the session, the origin,
    # the request) keeps the draft and its key, as no answer does.
    kept = "kept"
    assert [a["kind"] for a in out["answers"]] == [
        "unknown", "unknown", "known", "known", "known", kept, kept, kept, kept, kept]
    assert out["answers"][5]["text"] == (
        "Not started: the session ended. Sign in again, then activate.")
    assert out["answers"][6]["text"] == out["answers"][7]["text"] == (
        "Not started: Central refused the request from this page. Reload the console from "
        "the address you signed in at, then activate.")
    assert out["answers"][8]["text"] == "Not started: invalid_request. Nothing reached Central's Runtime."
    assert out["answers"][9]["text"] == "Not started: HTTP 404. Nothing reached Central's Runtime."
