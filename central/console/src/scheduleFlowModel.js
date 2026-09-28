/**
 * The Schedule flow's shape (flow design §7 J6): its steps, which step asks each field
 * (`FIELD_STEP`), its instance and route, its seed, and which of the Program rules
 * applies to a draft. Pure: no React; tests/test_console_schedule_flow.py drives it
 * under Node. The Program rules themselves (window, priority, the separate-windows
 * helper) stay in authoring.js.
 *
 * ONE WRITE, TWO SHAPES. A draft with "Number of windows" 1 (the default) schedules
 * ONE Program under its id; any other number is the separate-windows helper: that many
 * independent single-window Programs `<id>-1` … `<id>-<n>` (never a recurring rule,
 * design R2/Q2), checked by authoring.js `windowProblems` (an invalid number is its
 * reason there, never silently read as 1).
 *
 * INSTANCES. There are no edits: a draft is keyed `new` (`#/schedule/new/<step>`) and
 * opens at the Scene step, prefilled with the shell's `recentSceneId` (the Scene just
 * saved, or picked on a Scene card with "Schedule it").
 */

import { programProblems, windowProblems } from "./authoring.js";
import { flowKeys } from "./flow/instance.js";

/** @typedef {import("./flow/steps.js").Step} Step */

const SCENE = Object.freeze({ id: "scene", label: "Scene" });
const WHEN = Object.freeze({ id: "when", label: "When" });
const REVIEW = Object.freeze({ id: "review", label: "Review" });

/** @type {ReadonlyArray<Step>} */
export const SCHEDULE_STEPS = Object.freeze([SCENE, WHEN, REVIEW]);

/**
 * Which step asks each field of the Program problems (flow design §6). The name and
 * its id are asked on Review: the id derives from the name, and whether it (or, for
 * separate windows, each `<id>-<n>`) is free depends on the number of windows When
 * decided.
 */
export const SCHEDULE_FIELD_STEP = Object.freeze({
  scene: SCENE.id,
  start: WHEN.id,
  end: WHEN.id,
  weekdays: WHEN.id,
  count: WHEN.id,
  priority: REVIEW.id,
  name: REVIEW.id,
  id: REVIEW.id,
});

/** The fields that sit under their step's Advanced (they have a stated default). */
export const SCHEDULE_ADVANCED_FIELDS = Object.freeze(new Set(["weekdays", "count", "priority", "id"]));

/** The Schedule flow's instance and its routes (flow/instance.js `flowKeys`). */
export const SCHEDULE_KEYS = flowKeys({
  section: "schedule",
  firstStep: { create: SCENE.id },
  describe: { create: "a new Program" },
});

/** Every weekday ticked, indexed like `Date.getDay()` (0 = Sunday). */
export const EVERY_DAY = Object.freeze([true, true, true, true, true, true, true]);

/**
 * A new Program draft: no Scene, no window, one window, every weekday, priority 0
 * (the stated defaults, flow design §7 J6), no name.
 *
 * @typedef {{sceneId: string, start: string, end: string, count: string|number,
 *            weekdays: ReadonlyArray<boolean>, priority: string|number,
 *            name: string, idOverride: string|null}} ProgramDraft
 */
export const NEW_PROGRAM_DRAFT = Object.freeze({
  sceneId: "",
  start: "",
  end: "",
  count: 1,
  weekdays: EVERY_DAY,
  priority: 0,
  name: "",
  idOverride: null,
});

/**
 * The Schedule flow's seed: a new Program draft, its Scene prefilled with
 * `recentSceneId` when there is one.
 *
 * @param {string|null} recentSceneId
 * @returns {(key: string) => ProgramDraft}
 */
export function seedSchedule(recentSceneId) {
  const draft = recentSceneId === null ? NEW_PROGRAM_DRAFT : { ...NEW_PROGRAM_DRAFT, sceneId: recentSceneId };
  return () => draft;
}

/**
 * Whether the draft is the separate-windows helper: any "Number of windows" but 1.
 *
 * @param {ProgramDraft} draft
 * @returns {boolean}
 */
export function separateWindows(draft) {
  return String(draft.count).trim() !== "1";
}

/**
 * The problems of the write this draft makes: one Program's (authoring.js
 * `programProblems`), or the separate-windows helper's (`windowProblems`). `pendingIds`
 * are the window ids of this very draft's earlier, partly confirmed attempt: sending it
 * again sends only those not confirmed, so its own windows never read as collisions.
 *
 * @param {ProgramDraft} draft
 * @param {Set<string>} programIds the stored Program ids
 * @param {number} now Central's clock (`current.now`)
 * @param {ReadonlyArray<string>} [pendingIds]
 * @returns {import("./authoring.js").Problem[]}
 */
export function programDraftProblems(draft, programIds, now, pendingIds = []) {
  if (!separateWindows(draft)) {
    return programProblems(draft, programIds, now);
  }
  const pending = new Set(pendingIds);
  return windowProblems(draft, new Set([...programIds].filter((id) => !pending.has(id))), now);
}
