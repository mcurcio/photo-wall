/**
 * The Scene flow's shape (flow design §7 J4): its steps, which step asks each field
 * (`FIELD_STEP`), its instances and their routes, and its seed. Pure: no React;
 * tests/test_console_flow.py drives it under Node. The Scene's own rules (problems,
 * defaults, the lossless edit) stay in authoring.js.
 *
 * INSTANCES. A draft is keyed `new`, or `edit/<id>` for the stored Scene it edits
 * (flow/instance.js); a new Scene opens at Kind, an edit at Review.
 */

import { NEW_SCENE_DRAFT, PLAYBACK_LABELS, sceneEditDraft } from "./authoring.js";
import { sameValue } from "./flow/draftState.js";
import { editedId, flowKeys } from "./flow/instance.js";

/** @typedef {import("./flow/steps.js").Step} Step */

const KIND = Object.freeze({ id: "kind", label: "Kind" });
const PHOTOS = Object.freeze({ id: "photos", label: "Photos" });
const FRAMES = Object.freeze({ id: "frames", label: "Frames" });
const MEDIA = Object.freeze({ id: "media", label: "Media per frame" });
const PLAYBACK = Object.freeze({ id: "playback", label: "Playback" });
const REVIEW = Object.freeze({ id: "review", label: "Review" });

const LIVE_STEPS = Object.freeze([KIND, PHOTOS, FRAMES, PLAYBACK, REVIEW]);
const HAND_PICKED_STEPS = Object.freeze([KIND, PHOTOS, FRAMES, MEDIA, PLAYBACK, REVIEW]);

/**
 * The steps of a Scene of kind `mode`: "Media per frame" only for a hand-picked one.
 *
 * @param {"live"|"authored"} mode
 * @returns {ReadonlyArray<Step>}
 */
export function sceneSteps(mode) {
  return mode === "authored" ? HAND_PICKED_STEPS : LIVE_STEPS;
}

/**
 * Which step asks each field of authoring.js `sceneProblems` (flow design §6).
 * `media:<frame>` resolves through `media`.
 */
export const SCENE_FIELD_STEP = Object.freeze({
  mode: "kind",
  source: "photos",
  targets: "frames",
  media: "media",
  cycle: "playback",
  loop: "playback",
  name: "review",
  id: "review",
});

/** The fields that sit under their step's Advanced (they have a stated default). */
export const SCENE_ADVANCED_FIELDS = Object.freeze(new Set(["loop", "id"]));

/** The Scene flow's instances and their routes (flow/instance.js `flowKeys`). */
export const SCENE_KEYS = flowKeys({
  section: "scenes",
  firstStep: { create: KIND.id, edit: REVIEW.id },
  describe: { create: "a new Scene", edit: (id) => `Scene ${id}` },
});

/**
 * The Scene flow's seed over the stored definitions: `new` gets the defaults, an
 * edit the stored Scene (null when it is absent or the console cannot author it).
 *
 * @param {Record<string, object>} definitions
 * @returns {(key: string) => import("./authoring.js").SceneDraft|null}
 */
export function seedScene(definitions) {
  return (key) => {
    const id = editedId(key);
    if (id === null) {
      return NEW_SCENE_DRAFT;
    }
    const scene = definitions[id];
    return scene === undefined ? null : sceneEditDraft(scene);
  };
}

/**
 * The label of each authored value, by its problem field, as Review answers it and
 * Reload names it: the step's label, or the field's own for the playback values.
 */
export const SCENE_ANSWER_LABELS = Object.freeze({
  mode: KIND.label,
  source: PHOTOS.label,
  targets: FRAMES.label,
  media: MEDIA.label,
  cycle: PLAYBACK_LABELS.cycle,
  loop: PLAYBACK_LABELS.loop,
});

// The authored values a Reload can change, each by its answer label.
const RELOADED = [
  [SCENE_ANSWER_LABELS.mode, (draft) => draft.mode],
  [SCENE_ANSWER_LABELS.source, (draft) => draft.sourceRef],
  [SCENE_ANSWER_LABELS.targets, (draft) => [...draft.targets].sort()],
  [SCENE_ANSWER_LABELS.media, (draft) => draft.selections],
  [SCENE_ANSWER_LABELS.cycle, (draft) => Number(draft.cycleSeconds)],
  [SCENE_ANSWER_LABELS.loop, (draft) => draft.loop],
];

/**
 * The labels of the values that differ between two Scene drafts (Reload names them).
 *
 * @param {import("./authoring.js").SceneDraft} before
 * @param {import("./authoring.js").SceneDraft} after
 * @returns {string[]}
 */
export function changedSceneFields(before, after) {
  return RELOADED.filter(([, read]) => !sameValue(read(before), read(after))).map(([label]) => label);
}
