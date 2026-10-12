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

import functools
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import get_args

import pytest

from central.wall_plan.fit_settings import FIT_SETTING_RANGES
from central.wall_plan.selection import (
    MAX_NO_REPEAT_DAYS,
    Order,
    SceneSelection,
)

MIN_CROP_LIMIT, MAX_CROP_LIMIT = FIT_SETTING_RANGES["crop_limit"]

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
                    source_refs: ["holiday:1"], after_end: "keep_this_photo" }] } };
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
const hand = { ...scene.seedScene({})("new"), name: "x", mode: "authored", sourceRefs: ["holiday:1"],
               targets: ["lobby", "hall"] };
out.noMedia = authoring.sceneProblems({ ...hand, noMedia: ["lobby"] }, new Set())
  .map((p) => [p.field, p.message]);
out.loadingMedia = authoring.sceneProblems({ ...hand, noMedia: ["lobby"], loadingMedia: true },
  new Set()).map((p) => p.field);
out.cycleProblem = authoring.sceneProblems({ ...hand, mode: "live", cycleSeconds: "0" }, new Set())
  .map((p) => p.message);
out.fadeProblem = authoring.sceneProblems(
  { ...hand, mode: "live", cycleSeconds: 2, fadeSeconds: 2.5 }, new Set()).map((p) => [p.field, p.message]);
out.answerLabels = scene.SCENE_ANSWER_LABELS;

// --- Delivery 1x's settings: each one saved is read back by Edit (a lossless round trip).
const settings = { fadeSeconds: 3, ending: "fade", endingSeconds: 4.5, keepLastPhoto: false,
                   keepTogether: true };
const live = authoring.buildSave("live", { sceneId: "evening", sourceRefs: ["holiday:1"],
  targetIds: ["lobby"], cycleSeconds: 20, loop: true, ...settings }).body;
out.liveBody = live;
const pick = ({ fadeSeconds, ending, endingSeconds, keepLastPhoto, keepTogether }) =>
  ({ fadeSeconds, ending, endingSeconds, keepLastPhoto, keepTogether });
out.liveBack = pick(authoring.editableDraft(live));
const hands = authoring.buildSave("authored", { sceneId: "evening", sourceRefs: ["holiday:1"],
  targetIds: ["lobby"], cycleSeconds: 20, loop: true, selections: { lobby: "a1" },
  ...settings, ending: "black" }).body.scene;
out.authoredOutro = hands.outro_contributions;
out.authoredBack = pick(authoring.editableDraft(hands));
// Unequal fades (or a setting that differs per Frame) are not the console's: Edit is withheld.
out.unequal = authoring.editableDraft({ ...live, contributions: [
  { ...live.contributions[0], fade_in_seconds: 1 }] });
// Values the sliders cannot show exactly are not the console's either (no silent snapping).
const stretched = (fade, ending) => authoring.buildSave("live", { sceneId: "evening",
  sourceRefs: ["holiday:1"], targetIds: ["lobby"], cycleSeconds: 20, loop: true, ...settings,
  fadeSeconds: fade, endingSeconds: ending }).body;
out.outOfRange = [stretched(3, 12), stretched(1.3, 4.5), stretched(6, 4.5), stretched(3, 4.5)]
  .map((body) => authoring.editableDraft(body) !== null);
// "Fades out" brings the last photo back over half the fade, inside the ending.
out.endingProblem = authoring.sceneProblems({ ...hand, mode: "live", cycleSeconds: 20,
  fadeSeconds: 3, ending: "fade", endingSeconds: 1 }, new Set()).map((p) => [p.field, p.message]);

// --- Roadmap 5a: the Scene's selection, written, read back and bounded.
const chosen = { order: "chronological", noRepeatDays: "3", cropLimit: 0.3 };
const selected = authoring.buildSave("live", { sceneId: "evening", sourceRefs: ["holiday:1"],
  targetIds: ["lobby"], cycleSeconds: 20, loop: true, ...chosen }).body;
out.selectionBody = selected.selection;
const pickSelection = ({ order, noRepeatDays, cropLimit }) => ({ order, noRepeatDays, cropLimit });
out.selectionBack = pickSelection(authoring.editableDraft(selected));
out.selectionAuthoredBack = pickSelection(authoring.editableDraft(authoring.buildSave("authored", {
  sceneId: "evening", sourceRefs: ["holiday:1"], targetIds: ["lobby"], cycleSeconds: 20,
  loop: true, selections: { lobby: "a1" }, ...chosen }).body.scene));
// A stored Scene with no selection reads as the model's default.
const { selection: _stored, ...bare } = selected;
out.selectionMissing = pickSelection(authoring.decodeScene(bare));
// Values the controls cannot show exactly (a 33.3 % crop limit, half a day, out of range).
const withSelection = (selection) => ({ ...selected, selection: { ...selected.selection, ...selection } });
out.selectionEditable = [{ crop_limit: 0.333 }, { no_repeat_days: 2.5 }, { no_repeat_days: 400 },
  { crop_limit: 0.04 }, { order: "random" }, { crop_limit: 0.6, no_repeat_days: 365 },
  { crop_limit: 0.05, no_repeat_days: 0, order: "on_this_day" }]
  .map((selection) => authoring.editableDraft(withSelection(selection)) !== null);
out.noRepeatProblems = ["", "-1", "2.5", "366", "0", "365"].map((noRepeatDays) => authoring
  .sceneProblems({ ...hand, mode: "live", noRepeatDays }, new Set())
  .filter((p) => p.field === "noRepeatDays").map((p) => p.message));
out.ranges = authoring.SETTING_RANGES;
out.orders = authoring.ORDERS;
out.cropPercents = [0.05, 0.07, 0.35, 0.6].map(authoring.cropPercent);
console.log(JSON.stringify(out));
"""


@functools.cache
def _flow_out():
    """SCRIPT's answers, run once per process."""
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--",
         (SRC / "flow/draftState.js").as_uri(), (SRC / "flow/steps.js").as_uri(),
         (SRC / "sceneFlowModel.js").as_uri(), (SRC / "flow/instance.js").as_uri(),
         (SRC / "authoring.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def test_flow_kit_and_scene_flow_shape():
    out = _flow_out()

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

    # Frames before Photos (follow-ups C1): photos are chosen against known Frames.
    assert out["liveSteps"] == ["kind", "frames", "photos", "playback", "review"]
    assert out["handPickedSteps"] == ["kind", "frames", "photos", "media", "playback", "review"]
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
    assert out["advanced"] == ["id", "keepLastPhoto", "keepTogether", "loop"]
    # A new Scene's 1x settings at the settings catalogue's defaults (#41 1.5 s, #64 on,
    # #65 off); how it ends stays "none" (roadmap 1x: #62 and #63's defaults disagree).
    assert out["seedNew"] == {
        "mode": "live", "name": "", "idOverride": None, "sourceRefs": [], "targets": [],
        "selections": {}, "cycleSeconds": 30, "loop": True, "fadeSeconds": 1.5,
        "ending": "none", "endingSeconds": 3, "keepLastPhoto": True, "keepTogether": False,
        "order": "shuffle", "noRepeatDays": 7, "cropLimit": None, "revision": None}
    assert out["seedNewTarget"]["targets"] == ["frame_one"]
    assert out["seedNewBadTarget"]["targets"] == []
    assert out["seedEditWithTarget"] == out["seedEdit"]
    assert out["seedEdit"] == {
        "mode": "live", "name": "", "idOverride": None, "sourceRefs": ["holiday:1"],
        "targets": ["lobby"], "selections": {}, "cycleSeconds": 20, "loop": True,
        "fadeSeconds": 0, "ending": "none", "endingSeconds": 3, "keepLastPhoto": True,
        "keepTogether": False, "order": "shuffle", "noRepeatDays": 7, "cropLimit": None,
        "revision": 3}
    assert out["seedMissing"] is None
    assert out["changed"] == ["Keep repeating"]

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
    assert out["cycleProblem"] == ["Each photo shows for: choose more than 0 seconds."]
    assert out["answerLabels"] == {
        "mode": "Kind", "source": "Photos from", "targets": "Frames", "media": "Media per frame",
        "cycle": "Each photo shows for", "loop": "Keep repeating",
        "fade": "Fade between photos", "ending": "How it ends",
        "keepLastPhoto": "Keep the last photo up", "keepTogether": "Keep these Frames together",
        "order": "Order", "noRepeatDays": "Don't repeat within", "cropLimit": "Crop limit"}
    # A fade longer than the cycle is the fade's problem (the planner refuses it).
    assert out["fadeProblem"] == [
        ["fade", "Fade between photos must be no longer than each photo shows for."]]

    # Delivery 1x: each setting is written to the Scene model's own field...
    body = out["liveBody"]
    assert body["contributions"] == [{
        "target": "frame:lobby", "role": "lobby", "kind": "media", "source_refs": ["holiday:1"],
        "fade_in_seconds": 1.5, "fade_out_seconds": 1.5}]
    assert (body["outro_seconds"], body["protect_frames"]) == (4.5, True)
    assert body["outro_contributions"] == [{
        "target": "frame:lobby", "role": "lobby", "kind": "media", "source_refs": ["holiday:1"],
        "fade_in_seconds": 1.5, "fade_out_seconds": 3,  # back over half the fade, out over the rest
        "after_end": "keep_nothing"}]  # an ending keeps nothing after it
    assert out["authoredOutro"] == [
        {"target": "frame:lobby", "role": "lobby", "kind": "black", "after_end": "keep_nothing"}]
    # ...and Edit reads each back.
    settings = {"fadeSeconds": 3, "ending": "fade", "endingSeconds": 4.5,
                "keepLastPhoto": False, "keepTogether": True}
    assert out["liveBack"] == settings
    assert out["authoredBack"] == {**settings, "ending": "black"}
    assert out["unequal"] is None
    # A 12 s ending, a 1.3 s fade or a 6 s fade would be snapped by the sliders: not editable.
    assert out["outOfRange"] == [False, False, False, True]
    assert out["endingProblem"] == [
        ["endingSeconds", "Ending length must be at least half of Fade between photos."]]

    # Roadmap 5a: the selection is written as the model's fields (the crop limit a fraction,
    # the days a number), read back by Edit, and missing reads as the model's default.
    assert out["selectionBody"] == {"order": "chronological", "no_repeat_days": 3, "crop_limit": 0.3}
    assert SceneSelection.model_validate(out["selectionBody"])
    back = {"order": "chronological", "noRepeatDays": 3, "cropLimit": 0.3}
    assert out["selectionBack"] == back
    assert out["selectionAuthoredBack"] == back
    # Follow-ups B1: no crop limit of its own, the Scene follows the house value.
    assert out["selectionMissing"] == {"order": "shuffle", "noRepeatDays": 7, "cropLimit": None}
    assert out["selectionEditable"] == [False, False, False, False, False, True, True]
    rule = "Don't repeat within must be a whole number of days from 0 to 365."
    assert out["noRepeatProblems"] == [[rule], [rule], [rule], [rule], [], []]


def test_the_console_selection_bounds_are_the_models():
    """The console's selection controls offer exactly central/wall_plan/selection.py's range and
    choices (run ledger K1: the console mirrors the bounds)."""
    out = _flow_out()
    assert out["ranges"]["noRepeatDays"] == {"min": 0, "max": MAX_NO_REPEAT_DAYS, "step": 1}
    assert out["ranges"]["cropLimit"] == {"min": MIN_CROP_LIMIT, "max": MAX_CROP_LIMIT, "step": 0.01}
    assert out["orders"] == list(get_args(Order))
    assert out["cropPercents"] == [5, 7, 35, 60]


HAND_OFF_SCRIPT = r"""
const hand = await import(process.argv[1]);
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
console.log(JSON.stringify(out));
"""


def test_hand_offs():
    # Roadmap 4 (K2): the Source flow's steps are gone; the Photo source editor is one page
    # (tests/test_console_sources.py, tests/browser/test_source_flow_browser.py). The hand-off
    # from the Scene editor stays.
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", HAND_OFF_SCRIPT, "--", (SRC / "flow/handOff.js").as_uri()],
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
