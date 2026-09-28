/**
 * The Scene flow's shape (flow design §7 J4): its steps, which step asks each field
 * (`FIELD_STEP`), its instances and their routes, and its seed. Pure: no React;
 * tests/test_console_flow.py drives it under Node. The Scene's own rules (problems,
 * defaults, the lossless edit) stay in authoring.js.
 *
 * INSTANCES. A draft is keyed `new`, or `edit/<id>` for the stored Scene it edits.
 * (The design keys an edit by its stored id; the prefix keeps a Scene whose id is
 * "new" apart from a new Scene. Ids never hold a `/`.)
 */

import { NEW_SCENE_DRAFT, sceneEditDraft } from "./authoring.js";
import { sameValue } from "./flow/draftState.js";

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

export const NEW_KEY = "new";

/**
 * The draft key a route names, or null when the route is not a Scene flow.
 *
 * @param {import("./routes.js").Route|null} route
 * @returns {string|null}
 */
export function sceneKey(route) {
  if (route?.section !== "scenes") {
    return null;
  }
  if (route.flow === "new") {
    return NEW_KEY;
  }
  return route.flow === "edit" ? `edit/${route.id}` : null;
}

/**
 * The stored Scene id an edit key names, or null for a new Scene.
 *
 * @param {string|null} key
 * @returns {string|null}
 */
export function editedId(key) {
  return key?.startsWith("edit/") ? key.slice("edit/".length) : null;
}

/**
 * The route of step `step` of instance `key`.
 *
 * @param {string} key
 * @param {string} step
 * @returns {import("./routes.js").Route}
 */
export function sceneRoute(key, step) {
  const id = editedId(key);
  return id === null
    ? { section: "scenes", flow: "new", step }
    : { section: "scenes", id, flow: "edit", step };
}

/** Where an instance opens: a new Scene at Kind, an edit at Review. */
export function firstStep(key) {
  return editedId(key) === null ? KIND.id : REVIEW.id;
}

/** The instance in words: "a new Scene" or "Scene <id>". */
export function describeKey(key) {
  const id = editedId(key);
  return id === null ? "a new Scene" : `Scene ${id}`;
}

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

// The authored values a Reload can change, each by the label the flow shows it with.
const RELOADED = [
  ["Kind", (draft) => draft.mode],
  ["Photos", (draft) => draft.sourceRef],
  ["Frames", (draft) => [...draft.targets].sort()],
  ["Media per frame", (draft) => draft.selections],
  ["Seconds per cycle", (draft) => Number(draft.cycleSeconds)],
  ["Keep playing until the Program ends", (draft) => draft.loop],
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
