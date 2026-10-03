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
let result = draft.openDraft(state, "new", seed, 1);
out.openNew = { opened: result.opened, dirty: draft.isDirty(result.state),
                base: result.state.baseRevision, id: result.state.id };
state = draft.patchDraft(result.state, { name: "Evening" });
out.dirtyAfterPatch = draft.isDirty(state);
result = draft.openDraft(state, "edit/a", seed, 2);
out.refused = { opened: result.opened, same: result.state === state };
out.sameKeyKeeps = draft.openDraft(state, "new", seed, 3).state === state;
out.patchKeepsId = state.id;
out.closedId = draft.CLOSED.id;
// Discarded, then opened again under the same key: another draft, another id.
out.reopenedId = draft.openDraft(draft.CLOSED, "new", seed, 4).state.id;
out.backToSeed = draft.isDirty(draft.patchDraft(state, { name: "" }));
out.noopPatch = draft.patchDraft(state, { name: "Evening" }) === state;
out.fnPatch = draft.patchDraft(state, (value) => ({ name: value.name + "!" })).value.name;
out.nullPatch = draft.patchDraft(state, () => null) === state;
const clean = draft.openDraft(draft.CLOSED, "new", seed, 5).state;
result = draft.openDraft(clean, "edit/a", seed, 6);
out.cleanReplaced = { opened: result.opened, base: result.state.baseRevision, id: result.state.id };
let edited = draft.patchDraft(result.state, { name: "x" });
edited = draft.reseedDraft(edited, () => ({ name: "fresh", revision: 7 }));
out.reseeded = { base: edited.baseRevision, dirty: draft.isDirty(edited), name: edited.value.name,
                 id: edited.id };
// Held while its write is in flight: another key, a reseed and a close keep it, even
// clean; a patch still applies; released, it opens and closes as before.
const held = draft.holdDraft(clean, true);
result = draft.openDraft(held, "edit/a", seed, 7);
out.held = {
  refused: result.opened, same: result.state === held,
  reseedKeeps: draft.reseedDraft(held, () => ({ name: "fresh", revision: 9 })) === held,
  closeKeeps: draft.closeDraft(held) === held,
  patched: draft.patchDraft(held, { name: "y" }).value.name,
  released: draft.openDraft(draft.holdDraft(held, false), "edit/a", seed, 8).opened,
  closed: draft.closeDraft(draft.holdDraft(held, false)) === draft.CLOSED,
  closedNotHeld: draft.holdDraft(draft.CLOSED, true) === draft.CLOSED,
};
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
out.seedNewTarget = scene.seedScene(stored, "frame_one")("new");
out.seedNewBadTarget = scene.seedScene(stored, "old:frame")("new");
out.seedEditWithTarget = scene.seedScene(stored, "frame_one")("edit/evening");
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
    assert out["openNew"] == {"opened": "new", "dirty": False, "base": None, "id": 1}
    assert out["dirtyAfterPatch"] is True
    assert out["refused"] == {"opened": "new", "same": True}
    assert out["sameKeyKeeps"] is True
    # Identity: a patch keeps the draft's id, a closed draft has none, and the same key
    # opened again after a discard is another draft (a new id), so anything begun for
    # the first (an inline hand-off) is never taken for the second.
    assert out["patchKeepsId"] == 1
    assert out["closedId"] is None
    assert out["reopenedId"] == 4
    assert out["backToSeed"] is False  # dirty is structural, not "was ever patched"
    assert out["noopPatch"] is True and out["nullPatch"] is True
    assert out["fnPatch"] == "Evening!"
    assert out["cleanReplaced"] == {"opened": "edit/a", "base": 4, "id": 6}
    # Reload: a reseed takes the stored revision as the new base and is clean again; it
    # is the same draft.
    assert out["reseeded"] == {"base": 7, "dirty": False, "name": "fresh", "id": 6}
    # A write's hold (flow/useFlowWrite.js): the draft it was sent from stays open.
    assert out["held"] == {
        "refused": "new", "same": True, "reseedKeeps": True, "closeKeeps": True,
        "patched": "y", "released": "edit/a", "closed": True, "closedNotHeld": True}
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
    assert out["seedNewTarget"]["targets"] == ["frame_one"]
    assert out["seedNewBadTarget"]["targets"] == []
    assert out["seedEditWithTarget"] == out["seedEdit"]
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
            ["targets", "No compatible media for lobby in holiday. "
                    "Choose another frame, or another Source."],
        ["media:hall", "Choose media for hall."]]
    assert out["loadingMedia"] == ["media:lobby", "media:hall"]
    # One wording per label: the problem, Review's answers and Reload share it.
    assert out["cycleProblem"] == ["Seconds per cycle must be more than 0."]
    assert out["answerLabels"] == {
        "mode": "Kind", "source": "Photos", "targets": "Frames", "media": "Media per frame",
        "cycle": "Seconds per cycle", "loop": "Keep playing until the Program ends"}


SOURCE_SCRIPT = r"""
const hand = await import(process.argv[1]);
const source = await import(process.argv[2]);
const steps = await import(process.argv[3]);
const authoring = await import(process.argv[4]);
const out = {};

// --- Inline hand-offs (flow/handOff.js).
const first = hand.beginHandOff({ from: "scenes", to: "sources", label: "your Scene", owner: 7 }, 1);
out.begun = first;
out.orphaned = [
  hand.orphanedHandOff(first, "scenes", 7),     // its draft is still open
  hand.orphanedHandOff(first, "scenes", null),  // closed
  hand.orphanedHandOff(first, "scenes", 8),     // replaced, even under the same key
  hand.orphanedHandOff(first, "schedule", null), // another origin's
  hand.orphanedHandOff(null, "scenes", null),
];
out.to = [hand.handOffTo(first, "sources")?.id ?? null, hand.handOffTo(first, "schedule"),
          hand.handOffTo(null, "sources")];
const stale = hand.settleHandOff(first, 2);
out.stale = { same: stale.next === first, settled: stale.settled };
out.settled = hand.settleHandOff(first, 1);
out.none = hand.settleHandOff(null, 1);

// --- The Source flow's shape (sourceFlowModel.js).
out.steps = source.SOURCE_STEPS.map((step) => [step.id, step.label]);
const several = source.connectionRule(["a", "b"], []);
const one = source.connectionRule(["a"], []);
out.oneSteps = source.sourceSteps(one).map((step) => step.id);
out.fieldSteps = ["connection", "tags", "type", "favorites", "from", "until", "ref"]
  .map((field) => steps.stepOfField(source.sourceFieldStep(several), field));
out.oneConnectionStep = steps.stepOfField(source.sourceFieldStep(one), "connection");
out.oneFirst = source.sourceKeys(one).firstStep("new");
const K = source.sourceKeys(several);
out.keys = [K.fromRoute({ section: "sources", flow: "new", step: "name" }),
            K.fromRoute({ section: "sources", id: "spring", flow: "edit", step: "review" }),
            K.fromRoute({ section: "sources" }),
            K.fromRoute({ section: "scenes", flow: "new", step: "kind" })];
out.route = K.toRoute("new", K.firstStep("new"));
out.describe = K.describe("new");
out.editRoute = K.toRoute("edit/spring", K.firstStep("edit/spring"));
const spec = (ref) => ({ source_ref: "x", spec: ref === undefined ? {} : { connection_ref: ref } });
out.rules = [
  source.connectionRule(null, []),
  source.connectionRule(null, [spec("home"), spec("home"), spec(undefined)]),
  source.connectionRule(null, [spec("work"), spec("home")]),
  source.connectionRule([], []),
  source.connectionRule(["home"], [], "removed"),
];
out.advanced = [[], [spec("home")], [spec("a"), spec("b")]]
  .map((sources) => [...source.sourceAdvancedFields(source.connectionRule(null, sources))]);
out.seeds = [source.seedSource([], null)("new"), source.seedSource([spec("home")], null)("new").connectionRef,
             source.seedSource([spec("a"), spec("b")])("new").connectionRef];
out.seedEdit = source.seedSource([{ name: "spring", revision: 3, source_ref: "spring:3",
  spec: { connection_ref: "home", media_types: ["image"], favorites: true, tags: ["tag-1"] } }])("edit/spring");
out.answers = source.sourceAnswers({ ...source.NEW_SOURCE_DRAFT, favorites: "only",
                                     capturedFrom: "2024-01-01", sourceName: " spring " });
out.oneAnswers = source.sourceAnswers({ ...source.NEW_SOURCE_DRAFT, tags: ["t1", "t2"], connectionRef: "a" },
                                      one, { t1: "Family", t2: null }).map((row) => [row.field, row.value]);
out.problems = authoring.sourceProblems(source.NEW_SOURCE_DRAFT).map((p) => p.field);
out.spec = source.buildSourceSpec({ expectedRevision: 2, connectionRef: "home",
                                    mediaType: "image", favorites: "not" });
out.specBoth = source.buildSourceSpec({ connectionRef: "h", mediaType: "both" });
out.specTagged = source.buildSourceSpec({ connectionRef: "h", mediaType: "image", tags: out.seedEdit.tags });
console.log(JSON.stringify(out));
"""


def test_hand_offs_and_source_flow_shape():
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SOURCE_SCRIPT, "--",
         (SRC / "flow/handOff.js").as_uri(), (SRC / "sourceFlowModel.js").as_uri(),
         (SRC / "flow/steps.js").as_uri(), (SRC / "authoring.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True)
    out = json.loads(result.stdout)

    # A hand-off is addressed to one section and settles once, by its own id only.
    assert out["begun"] == {
        "id": 1, "from": "scenes", "to": "sources", "label": "your Scene", "owner": 7}
    # The origin settles it with null once the draft it was begun for is no longer open.
    assert out["orphaned"] == [False, True, True, False, False]
    assert out["to"] == [1, None, None]
    assert out["stale"] == {"same": True, "settled": False}
    assert out["settled"] == {"next": None, "settled": True}
    assert out["none"] == {"next": None, "settled": False}

    assert out["steps"] == [["library", "Library"], ["tags", "Tags"], ["narrow", "Narrow"],
                            ["name", "Name"], ["review", "Review"]]
    # One known connection: no connection step; it sits under Name's Advanced, and a new
    # Source opens on Choose tags.
    assert out["oneSteps"] == ["tags", "narrow", "name", "review"]
    assert out["fieldSteps"] == ["library", "tags", "narrow", "narrow", "narrow", "narrow", "name"]
    assert out["oneConnectionStep"] == "name"
    assert out["oneFirst"] == "tags"
    assert out["keys"] == ["new", "edit/spring", None, None]
    assert out["route"] == {"section": "sources", "flow": "new", "step": "library"}
    assert out["editRoute"] == {"section": "sources", "id": "spring", "flow": "edit", "step": "review"}
    assert out["describe"] == "a new Source"
    # The legacy connection rule keeps manual entry until the worker reports. Explicit
    # empty and removed saved connections are represented separately from unknown.
    assert out["rules"] == [
        {"shown": "field", "values": [], "prefill": "", "reported": False, "selectedUnavailable": False},
        {"shown": "advanced", "values": ["home"], "prefill": "home", "reported": False, "selectedUnavailable": False},
        {"shown": "chooser", "values": ["home", "work"], "prefill": "", "reported": False, "selectedUnavailable": False},
        {"shown": "blocked", "values": [], "prefill": "", "reported": True, "selectedUnavailable": False},
        {"shown": "chooser", "values": ["home"], "prefill": "", "reported": True, "selectedUnavailable": True}]
    assert out["advanced"] == [[], ["connection"], []]
    assert out["seeds"] == [
        {"mediaType": "both", "favorites": "any", "capturedFrom": "", "capturedUntil": "",
         "sourceName": "", "connectionRef": "", "newConnection": False, "tags": []}, "home", ""]
    assert out["seedEdit"] == {
        "mediaType": "image", "favorites": "only", "capturedFrom": "", "capturedUntil": "",
        "sourceName": "spring", "connectionRef": "home", "newConnection": False, "tags": ["tag-1"],
        "revision": 3}
    assert out["answers"] == [
        {"label": "Connection name", "field": "connection", "value": None},
        {"label": "Tags in your library", "field": "tags", "value": "No tags: everything on your library's timeline only (not archived, hidden or other users' media)"},
        {"label": "Media type", "field": "type", "value": "Photos and videos"},
        {"label": "Favourites", "field": "favorites", "value": "Only favourites"},
        {"label": "Dated from", "field": "from", "value": "2024-01-01"},
        {"label": "Dated until", "field": "until", "value": "No limit"},
        {"label": "Source name", "field": "ref", "value": "spring"}]
    # The one connection is answered last (under Name's Advanced); a gone tag says so.
    assert out["oneAnswers"][0] == ["tags", "Family and a tag that no longer exists in your library"]
    assert out["oneAnswers"][-1] == ["connection", "a"]
    assert out["problems"] == ["ref", "connection"]
    assert out["spec"] == {"expected_revision": 2, "connection_ref": "home",
                           "media_types": ["image"], "favorites": False}
    assert out["specBoth"]["media_types"] == ["image", "video"]
    assert "favorites" not in out["specBoth"] and "captured_from" not in out["specBoth"]
    # A saved Source's tags travel through its draft to the write; an untagged one sends none.
    assert out["specTagged"]["tags"] == ["tag-1"] and "tags" not in out["spec"]


def test_source_health_separates_unattempted_from_failed_first_refresh():
    _require_node()
    script = r"""
const health = await import(process.argv[1]);
const base = { spec: {}, next_refresh: 0, last_success: null };
console.log(JSON.stringify([
  health.sourceState({ ...base, status: "unavailable", refresh_completed_revision: 0 }, 1000, false),
  health.sourceState({ ...base, status: "incompatible", refresh_completed_revision: 1 }, 1000, false),
]));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, "--", (SRC / "mediaHealth.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True)
    states = json.loads(result.stdout)
    assert states[0]["state"] == "never-refreshed"
    assert states[0]["label"] == "Awaiting refresh"
    assert states[1]["state"] == "failing"
    # No code: the status alone names no owner, so it reads neutrally (§39 R21).
    assert states[1]["label"] == "Refresh failed (incompatible) · never refreshed successfully"


def test_source_health_qualifies_partial_refresh_without_marking_it_failed():
    _require_node()
    script = r"""
const health = await import(process.argv[1]);
const base = { spec: {}, next_refresh: 1100, last_success: 940,
  status: "ok", refresh_completed_revision: 1 };
console.log(JSON.stringify([
  health.sourceState({ ...base, counts: { valid: 3, pending: 2, rejected: 1 },
    diagnostics: [{ code: "metadata_invalid" }, { code: "metadata_pending_or_changed" }] }, 1000, false),
  health.sourceState({ ...base, counts: { valid: 3, pending: 0, rejected: 0 }, diagnostics: [] }, 1000, false),
]));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, "--", (SRC / "mediaHealth.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True)
    partial, healthy = json.loads(result.stdout)
    assert partial["state"] == "ok"
    assert partial["severity"] == "ok"
    assert partial["label"] == ("Your photo library last reported 1 min ago · the media worker accepted 3 "
                                "in that refresh · 3 items pending or rejected")
    assert healthy["state"] == "ok"
    assert healthy["label"] == "Your photo library last reported 1 min ago · the media worker accepted 3 in that refresh"


def test_program_edit_retains_later_repeated_hour_occurrence():
    _require_node()
    script = r"""
const model = await import(process.argv[1]);
const source = { program_id: "fold", scene_id: "night", starts_at: 1793525400,
                 ends_at: 1793526300, priority: 0 };
const draft = model.programEditDraft(source);
const retained = model.effectiveProgramTimes(draft);
const changed = model.effectiveProgramTimes({ ...draft, start: "2026-11-01T01:15" });
const invalidChange = { ...draft, start: "2026-11-01T02:45" };
console.log(JSON.stringify({ draft, retained, changed,
  problems: model.programDraftProblems(draft, new Set(["fold"]), 1000, [], true),
  changedProblems: model.programDraftProblems(invalidChange, new Set(["fold"]), 1000, [], true) }));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, "--", (SRC / "scheduleFlowModel.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True,
        env={**os.environ, "TZ": "America/Los_Angeles"})
    out = json.loads(result.stdout)
    assert out["draft"]["start"] == "2026-11-01T01:30"
    assert out["retained"]["startsAt"] == 1793525400
    assert out["retained"]["endsAt"] == 1793526300
    assert out["retained"]["retainedStart"] is True
    assert out["retained"]["retainedEnd"] is True
    assert out["changed"]["startsAt"] == 1793520900
    assert out["changed"]["retainedStart"] is False
    assert not any(problem["field"] == "end" and "before it starts" in problem["message"]
                   for problem in out["problems"])
    assert any(problem["field"] == "end" and "before it starts" in problem["message"]
               for problem in out["changedProblems"])
