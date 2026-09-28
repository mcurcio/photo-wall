"""The console's flow kit and the Scene flow's shape, as pure modules (flow design §6, §7;
bead 2): the one-draft-per-flow invariant, reseed and its base revision, step order and
problem routing, instances, places and focus views, and the Scene flow's steps, keys, seed
and problems.

Runs the modules under Node (they are pure: no React), which the console build already
requires, the way tests/test_console_routes_r4.py runs routes.js. The browser half is
tests/browser/test_scene_flow_browser.py. Without Node it skips on a developer machine,
but FAILS where the checks are meant to run in full (`CI` or `PHOTO_WALL_BROWSER_TESTS`
set), so a missing Node never reads as a pass there.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).parents[1] / "central/console/src"


def _require_node():
    if shutil.which("node") is not None:
        return
    if os.environ.get("CI") or os.environ.get("PHOTO_WALL_BROWSER_TESTS"):
        pytest.fail("Node (the console build's) is absent where the console checks must run")
    pytest.skip("Node (the console build's) is absent")


SCRIPT = r"""
const draft = await import(process.argv[1]);
const steps = await import(process.argv[2]);
const scene = await import(process.argv[3]);
const instance = await import(process.argv[4]);
const authoring = await import(process.argv[5]);
const out = {};

// --- One draft per flow.
const seed = (key) => (key === "new" ? { name: "", revision: null } : { name: key, revision: 4 });
let state = draft.CLOSED;
let result = draft.openDraft(state, "new", seed);
out.openNew = { opened: result.opened, dirty: draft.isDirty(result.state),
                base: result.state.baseRevision };
state = draft.patchDraft(result.state, { name: "Evening" });
out.dirtyAfterPatch = draft.isDirty(state);
result = draft.openDraft(state, "edit/a", seed);
out.refused = { opened: result.opened, same: result.state === state };
out.sameKeyKeeps = draft.openDraft(state, "new", seed).state === state;
out.backToSeed = draft.isDirty(draft.patchDraft(state, { name: "" }));
out.noopPatch = draft.patchDraft(state, { name: "Evening" }) === state;
out.fnPatch = draft.patchDraft(state, (value) => ({ name: value.name + "!" })).value.name;
out.nullPatch = draft.patchDraft(state, () => null) === state;
const clean = draft.openDraft(draft.CLOSED, "new", seed).state;
result = draft.openDraft(clean, "edit/a", seed);
out.cleanReplaced = { opened: result.opened, base: result.state.baseRevision };
let edited = draft.patchDraft(result.state, { name: "x" });
edited = draft.reseedDraft(edited, () => ({ name: "fresh", revision: 7 }));
out.reseeded = { base: edited.baseRevision, dirty: draft.isDirty(edited), name: edited.value.name };
out.sameValue = [
  draft.sameValue({ a: [1, { b: 2 }], c: "x" }, { c: "x", a: [1, { b: 2 }] }),
  draft.sameValue([1, 2], [2, 1]),
  draft.sameValue({ a: 1 }, { a: 1, b: undefined }),
];

// --- Step order and problem routing.
const S = [{ id: "a", label: "A" }, { id: "b", label: "B" }, { id: "c", label: "C" },
           { id: "review", label: "Review" }];
const FIELDS = { one: "a", two: "b", media: "c", name: "review" };
out.stepOfField = [steps.stepOfField(FIELDS, "two"), steps.stepOfField(FIELDS, "media:x:y"),
                   steps.stepOfField(FIELDS, "nope") ?? null];
out.ordered = steps.inStepOrder(
  [{ field: "name" }, { field: "media:f" }, { field: "one" }, { field: "two" }],
  FIELDS, S).map((p) => p.field);
out.own = steps.problemsOf([{ field: "one" }, { field: "two" }], FIELDS, "b").map((p) => p.field);
out.next = [
  steps.nextStep(S, "a"),
  steps.nextStep(S, "a", { returning: true, problemSteps: new Set(["c"]) }),
  steps.nextStep(S, "a", { returning: true, problemSteps: new Set(["a"]) }),
  steps.nextStep(S, "review"),
];
out.previous = [steps.previousStep(S, "a"), steps.previousStep(S, "c")];

// --- The Scene flow's shape.
out.liveSteps = scene.sceneSteps("live").map((s) => s.id);
out.handPickedSteps = scene.sceneSteps("authored").map((s) => s.id);
const K = scene.SCENE_KEYS;
out.keys = [
  K.fromRoute({ section: "scenes", flow: "new", step: "kind" }),
  K.fromRoute({ section: "scenes", id: "new", flow: "edit", step: "review" }),
  K.fromRoute({ section: "scenes" }),
  K.fromRoute({ section: "now", flow: "show", step: "scene" }),
  K.fromRoute(null),
];
out.routes = [K.toRoute("new", "frames"), K.toRoute("edit/new", "review")];
out.first = [K.firstStep("new"), K.firstStep("edit/a")];
out.describe = [K.describe("new"), K.describe("edit/new")];

// --- The flow kit's instances (flow/instance.js).
const show = instance.flowKeys({ section: "now", newFlow: "show", firstStep: { create: "scene" },
                                 describe: { create: "a new activation" } });
out.showKeys = [
  show.fromRoute({ section: "now", flow: "show", step: "review" }),
  show.fromRoute({ section: "now", id: "x", flow: "edit", step: "review" }),
  show.fromRoute({ section: "now" }),
];
out.showRoute = show.toRoute("new", "review");
out.edited = [instance.editedId("edit/a/b"), instance.editedId("new"), instance.editedId(null),
              instance.editKey("x")];
const place = (routeKey, available, draftKey, dirty) =>
  instance.instancePlace({ routeKey, available, draftKey, dirty });
out.places = [
  place(null, "ok", "new", true),
  place("edit/a", "missing", "edit/a", true),
  place("edit/a", "unavailable", null, false),
  place("new", "ok", "new", false),
  place("edit/a", "ok", "new", true),
  place("edit/a", "ok", "new", false),
  place("edit/a", "ok", null, false),
];
const LIVE = scene.sceneSteps("live");
out.shown = [
  instance.shownStep("open", LIVE, "frames"),
  instance.shownStep("open", LIVE, "media"),
  instance.shownStep("open", LIVE, undefined),
  instance.shownStep("blocked", LIVE, "frames"),
];
out.fieldSteps = ["mode", "source", "targets", "media:lobby", "cycle", "loop", "name", "id"]
  .map((field) => steps.stepOfField(scene.SCENE_FIELD_STEP, field));
out.advanced = [...scene.SCENE_ADVANCED_FIELDS].sort();
const stored = { evening: { scene_id: "evening", revision: 3, cycle_seconds: 20, loop: true,
  contributions: [{ target: "frame:lobby", role: "lobby", kind: "media",
                    source_refs: ["holiday:1"], retain_on_expiry: true }] } };
const seedOf = scene.seedScene(stored);
out.seedNew = seedOf("new");
out.seedEdit = seedOf("edit/evening");
out.seedMissing = seedOf("edit/ghost");
out.changed = scene.changedSceneFields(seedOf("edit/evening"),
  { ...seedOf("edit/evening"), cycleSeconds: "20", targets: ["lobby"], loop: false });

// --- Focus views and finishing (flow/instance.js).
out.views = [
  instance.flowView({ shown: false, place: "open", routeKey: "new", step: "frames" }),
  instance.flowView({ shown: true, place: "open", routeKey: "new", step: "frames" }),
  instance.flowView({ shown: true, place: "open", routeKey: "new", step: null }),
  instance.flowView({ shown: true, place: "opening", routeKey: "edit/a", step: null }),
  instance.flowView({ shown: true, place: "missing", routeKey: "edit/a", step: null }),
  instance.flowView({ shown: true, place: "list", routeKey: null, step: null }),
];
out.matches = [
  instance.viewMatches(instance.stepView("new", "frames"), "open:new:frames"),
  instance.viewMatches(instance.stepView("new", "frames"), "open:new:playback"),
  instance.viewMatches(instance.stepView("edit/a"), "open:edit/a:review"),
  instance.viewMatches(instance.stepView("edit/a"), "open:edit/ab:review"),
  instance.viewMatches(instance.stepView("edit/a"), "missing:edit/a"),
];
out.here = [
  instance.hashNamesInstance(K, "#/scenes/new/review", "new"),
  instance.hashNamesInstance(K, "#/scenes/new/kind", "new"),
  instance.hashNamesInstance(K, "#/now", "new"),
  instance.hashNamesInstance(K, "#/scenes", "new"),
  instance.hashNamesInstance(K, "#/scenes/evening/edit/review", "edit/evening"),
  instance.hashNamesInstance(K, "#/scenes/evening/edit/review", "new"),
  instance.hashNamesInstance(K, "#/scenes/new/review", null),
];

// --- The Scene's problems and labels (authoring.js).
const hand = { ...scene.seedScene({})("new"), name: "x", mode: "authored", sourceRef: "holiday:1",
               targets: ["lobby", "hall"] };
out.noMedia = authoring.sceneProblems({ ...hand, noMedia: ["lobby"] }, new Set())
  .map((p) => [p.field, p.message]);
out.loadingMedia = authoring.sceneProblems({ ...hand, noMedia: ["lobby"], loadingMedia: true },
  new Set()).map((p) => p.field);
out.cycleProblem = authoring.sceneProblems({ ...hand, mode: "live", cycleSeconds: "0" }, new Set())
  .map((p) => p.message);
out.answerLabels = scene.SCENE_ANSWER_LABELS;
console.log(JSON.stringify(out));
"""


def test_flow_kit_and_scene_flow_shape():
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--",
         (SRC / "flow/draftState.js").as_uri(), (SRC / "flow/steps.js").as_uri(),
         (SRC / "sceneFlowModel.js").as_uri(), (SRC / "flow/instance.js").as_uri(),
         (SRC / "authoring.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True)
    out = json.loads(result.stdout)

    # One draft per flow: a dirty draft refuses another key and the open key is returned.
    assert out["openNew"] == {"opened": "new", "dirty": False, "base": None}
    assert out["dirtyAfterPatch"] is True
    assert out["refused"] == {"opened": "new", "same": True}
    assert out["sameKeyKeeps"] is True
    assert out["backToSeed"] is False  # dirty is structural, not "was ever patched"
    assert out["noopPatch"] is True and out["nullPatch"] is True
    assert out["fnPatch"] == "Evening!"
    assert out["cleanReplaced"] == {"opened": "edit/a", "base": 4}
    # Reload: a reseed takes the stored revision as the new base and is clean again.
    assert out["reseeded"] == {"base": 7, "dirty": False, "name": "fresh"}
    assert out["sameValue"] == [True, False, False]

    assert out["stepOfField"] == ["b", "c", None]
    assert out["ordered"] == ["one", "two", "media:f", "name"]
    assert out["own"] == ["two"]
    # Returning toward Review: the next step with a problem, else Review.
    assert out["next"] == ["b", "c", "review", "review"]
    assert out["previous"] == [None, "b"]

    assert out["liveSteps"] == ["kind", "photos", "frames", "playback", "review"]
    assert out["handPickedSteps"] == ["kind", "photos", "frames", "media", "playback", "review"]
    # A Scene whose id is "new" is an edit key of its own, never the new Scene.
    assert out["keys"] == ["new", "edit/new", None, None, None]
    assert out["routes"] == [
        {"section": "scenes", "flow": "new", "step": "frames"},
        {"section": "scenes", "id": "new", "flow": "edit", "step": "review"}]
    assert out["first"] == ["kind", "review"]
    assert out["describe"] == ["a new Scene", "Scene new"]
    # The kit's keys serve a flow with another segment and no edits (Show now).
    assert out["showKeys"] == ["new", None, None]
    assert out["showRoute"] == {"section": "now", "flow": "show", "step": "review"}
    assert out["edited"] == ["a/b", None, None, "edit/x"]
    # What the section shows: another section or the list; a gone or unauthorable edit;
    # the open instance; another one, blocked by a dirty draft or opening over a clean one.
    assert out["places"] == [
        "list", "missing", "unavailable", "open", "blocked", "opening", "opening"]
    # A step not among the draft's steps (Media for a live Scene) is normalised away.
    assert out["shown"] == ["frames", None, None, None]
    assert out["fieldSteps"] == [
        "kind", "photos", "frames", "media", "playback", "playback", "review", "review"]
    assert out["advanced"] == ["id", "loop"]
    assert out["seedNew"] == {
        "mode": "live", "name": "", "idOverride": None, "sourceRef": "", "targets": [],
        "selections": {}, "cycleSeconds": 30, "loop": True, "revision": None}
    assert out["seedEdit"] == {
        "mode": "live", "name": "", "idOverride": None, "sourceRef": "holiday:1",
        "targets": ["lobby"], "selections": {}, "cycleSeconds": 20, "loop": True, "revision": 3}
    assert out["seedMissing"] is None
    assert out["changed"] == ["Keep playing until the Program ends"]

    # Focus views: away while another section shows; a step; between views (normalising,
    # opening) none; any other place by its name.
    assert out["views"] == [
        "away", "open:new:frames", None, None, "missing:edit/a", "list:"]
    # A step view without a step names every step of that instance, and only it.
    assert out["matches"] == [True, False, True, False, False]
    # Finish may take the history entry only while the location names the instance.
    assert out["here"] == [True, True, False, False, True, False, False]

    # A hand-picked frame with nothing to choose is a frame problem, said with its Source;
    # while candidates load, that is what each frame says.
    assert out["noMedia"] == [
        ["targets", "No compatible media for lobby in holiday:1. "
                    "Choose another frame, or another Source."],
        ["media:hall", "Choose media for hall."]]
    assert out["loadingMedia"] == ["media:lobby", "media:hall"]
    # One wording per label: the problem, Review's answers and Reload share it.
    assert out["cycleProblem"] == ["Seconds per cycle must be more than 0."]
    assert out["answerLabels"] == {
        "mode": "Kind", "source": "Photos", "targets": "Frames", "media": "Media per frame",
        "cycle": "Seconds per cycle", "loop": "Keep playing until the Program ends"}
