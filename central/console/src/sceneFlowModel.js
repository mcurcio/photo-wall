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
import { FRAME_ID_PATTERN } from "./frameIds.js";
import { routeIdName } from "./routes.js";
import { SCENE, schedule, scheduleName } from "./showWords.js";

/** @typedef {import("./flow/steps.js").Step} Step */

const KIND = Object.freeze({ id: "kind", label: "Kind" });
const PHOTOS = Object.freeze({ id: "photos", label: "Photos" });
const FRAMES = Object.freeze({ id: "frames", label: "Frames" });
const MEDIA = Object.freeze({ id: "media", label: "Media per frame" });
const PLAYBACK = Object.freeze({ id: "playback", label: "Playback" });
const REVIEW = Object.freeze({ id: "review", label: "Review" });

// Frames before Photos (follow-ups C1): photos are chosen against known Frames, so the Photos
// step can show which of them fit (owner, 2026-10-11, the preview of "transient Frame
// compatibility").
const LIVE_STEPS = Object.freeze([KIND, FRAMES, PHOTOS, PLAYBACK, REVIEW]);
const HAND_PICKED_STEPS = Object.freeze([KIND, FRAMES, PHOTOS, MEDIA, PLAYBACK, REVIEW]);

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
  transition: "playback",
  fade: "playback",
  ending: "playback",
  endingSeconds: "playback",
  keepLastPhoto: "playback",
  keepTogether: "playback",
  order: "playback",
  noRepeatDays: "playback",
  cropLimit: "playback",
  name: "review",
  id: "review",
});

/** The fields that sit under their step's Advanced (they have a stated default). */
export const SCENE_ADVANCED_FIELDS = Object.freeze(
  new Set(["loop", "keepLastPhoto", "keepTogether", "id"]),
);

/** The Scene flow's instances and their routes (flow/instance.js `flowKeys`). */
export const SCENE_KEYS = flowKeys({
  section: "scenes",
  firstStep: { create: KIND.id, edit: REVIEW.id },
  // An edit route's id is named only when it is an id (a typed URL may hold any text).
  describe: { create: "a new Scene", edit: (id) => routeIdName("Scene", id) },
});

/**
 * The Scene flow's seed over the stored definitions: `new` gets the defaults, an
 * edit the stored Scene (null when it is absent or the console cannot author it).
 *
 * @param {Record<string, object>} definitions
 * @returns {(key: string) => import("./authoring.js").SceneDraft|null}
 */
export function seedScene(definitions, initialTarget = null) {
  const target = typeof initialTarget === "string" && FRAME_ID_PATTERN.test(initialTarget)
    ? initialTarget
    : null;
  return (key) => {
    const id = editedId(key);
    if (id === null) {
      // The Frame is an initial answer for a newly seeded draft only. Returning a
      // fresh object keeps the shared default immutable and edits remain untouched.
      return target === null
        ? NEW_SCENE_DRAFT
        : { ...NEW_SCENE_DRAFT, targets: [target] };
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
  source: "Photos from",
  targets: FRAMES.label,
  media: MEDIA.label,
  cycle: PLAYBACK_LABELS.cycle,
  loop: PLAYBACK_LABELS.loop,
  fade: PLAYBACK_LABELS.fade,
  ending: PLAYBACK_LABELS.ending,
  keepLastPhoto: PLAYBACK_LABELS.keepLastPhoto,
  keepTogether: PLAYBACK_LABELS.keepTogether,
  order: PLAYBACK_LABELS.order,
  noRepeatDays: PLAYBACK_LABELS.noRepeatDays,
  cropLimit: PLAYBACK_LABELS.cropLimit,
});

// The authored values a Reload can change, each by its answer label.
const RELOADED = [
  [SCENE_ANSWER_LABELS.mode, (draft) => draft.mode],
  [SCENE_ANSWER_LABELS.source, (draft) => draft.sourceRefs],
  [SCENE_ANSWER_LABELS.targets, (draft) => [...draft.targets].sort()],
  [SCENE_ANSWER_LABELS.media, (draft) => draft.selections],
  [SCENE_ANSWER_LABELS.cycle, (draft) => Number(draft.cycleSeconds)],
  [SCENE_ANSWER_LABELS.loop, (draft) => draft.loop],
  // The Review answer names the style with the seconds ("Cross-fade, 1.5 s"): one value.
  [SCENE_ANSWER_LABELS.fade, (draft) => `${draft.transitionStyle}:${Number(draft.fadeSeconds)}`],
  [SCENE_ANSWER_LABELS.ending, (draft) =>
    (draft.ending === "none" ? "none" : `${draft.ending}:${Number(draft.endingSeconds)}`)],
  [SCENE_ANSWER_LABELS.keepLastPhoto, (draft) => draft.keepLastPhoto],
  [SCENE_ANSWER_LABELS.keepTogether, (draft) => draft.keepTogether],
  [SCENE_ANSWER_LABELS.order, (draft) => draft.order],
  [SCENE_ANSWER_LABELS.noRepeatDays, (draft) => Number(draft.noRepeatDays)],
  [SCENE_ANSWER_LABELS.cropLimit, (draft) => Number(draft.cropLimit)],
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

/**
 * What still plays an earlier version of a just-saved Scene, in the operator's words: each
 * Schedule by its name, "Show now", and "a waiting Show now" for a queued one (a Scene save's
 * answer, central/runtime.py `SceneOnWall.earlier_version`). Each name once; empty when the
 * wall already follows the saved Scene (or the answer carries none).
 *
 * @param {{playing?: {run_id: string, started_by: "schedule"|"show_now"}[], waiting?: string[]}|null|undefined} earlier
 * @param {object|null|undefined} runtime the served runtime (snapshot.runtime): a Schedule's
 *   Run names its Program there
 * @returns {string[]}
 */
export function earlierVersionNames(earlier, runtime) {
  const runs = runtime?.current?.runs ?? [];
  const named = (ref) => {
    if (ref.started_by !== "schedule") {
      return SCENE.earlierShowNow;
    }
    const programId = runs.find((run) => run.run_id === ref.run_id)?.program_id ?? null;
    return programId === null ? SCENE.earlierSchedule : schedule("named", { name: scheduleName(runtime, programId) });
  };
  const names = [
    ...(earlier?.playing ?? []).map(named),
    ...((earlier?.waiting ?? []).length > 0 ? [SCENE.earlierWaiting] : []),
  ];
  return [...new Set(names)];
}
