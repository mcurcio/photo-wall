/**
 * The Schedule flow's shape (flow design §7 J6; Power and the week, roadmap 2b): its steps,
 * which step asks each field (`SCHEDULE_FIELD_STEP`), its instance and route, its seed, the
 * problems a draft has before it is sent, and the body it sends. Pure: no React;
 * tests/test_console_schedule_flow.py drives it under Node.
 *
 * A Program is a Scene, a weekly When on the house clock and a priority (central/runtime.py
 * `ProgramDraft`): Central works out every window from the When in the house's time zone, so
 * the console sends clock times ("17:00") or sunrise and sunset with minutes, and works out no
 * instant. Central names the Program
 * (`program_id`) at create; an edit sends the whole Program it loaded (`expected`) and the
 * draft that replaces it, and applies from the next window (the one playing keeps its end).
 *
 * INSTANCES. New drafts open at Scene with the shell's `recentScene`; edits are keyed by
 * Program id and open at Review.
 */

import { editedId, flowKeys } from "./flow/instance.js";
import { SCHEDULE } from "./showWords.js";

/** @typedef {import("./flow/steps.js").Step} Step */

const SCENE = Object.freeze({ id: "scene", label: "Scene" });
const WHEN = Object.freeze({ id: "when", label: "When" });
const REVIEW = Object.freeze({ id: "review", label: "Review" });

/** @type {ReadonlyArray<Step>} */
export const SCHEDULE_STEPS = Object.freeze([SCENE, WHEN, REVIEW]);

/** Which step asks each field (flow design §6). */
export const SCHEDULE_FIELD_STEP = Object.freeze({
  scene: SCENE.id,
  days: WHEN.id,
  start: WHEN.id,
  end: WHEN.id,
  priority: REVIEW.id,
  name: REVIEW.id,
});

/** The fields that sit under their step's Advanced (they have a stated default). */
export const SCHEDULE_ADVANCED_FIELDS = Object.freeze(new Set(["priority"]));

/** The Schedule flow's instance and its routes (flow/instance.js `flowKeys`). */
export const SCHEDULE_KEYS = flowKeys({
  section: "schedule",
  firstStep: { create: SCENE.id, edit: REVIEW.id },
  describe: { create: SCHEDULE.describeNew, edit: () => SCHEDULE.describeEdit },
});

/** The days of a When, Monday first, as Central names them (central/house_time/model.py). */
export const DAY_KEYS = Object.freeze(["mon", "tue", "wed", "thu", "fri", "sat", "sun"]);

/** Every day ticked, Monday first. */
export const EVERY_DAY = Object.freeze([true, true, true, true, true, true, true]);

/** The priorities, weakest first, with their words (#78; "normal" is the default). */
export const PRIORITIES = Object.freeze([
  ["normal", "Normal"],
  ["special-event", "Special event"],
  ["always-wins", "Always wins"],
]);

/** Where a start or end sits (#74, #75): a clock time, or sunrise or sunset plus or minus minutes. */
export const TIME_ATS = Object.freeze([
  ["clock", "Clock time"],
  ["sunrise", "Sunrise"],
  ["sunset", "Sunset"],
]);

/**
 * A new Program draft: no Scene, every day, no times (clock times), Normal priority, no name.
 * A start or end is three fields: where it sits (`startAt`: "clock", "sunrise", "sunset"), its
 * clock time (`start`, "17:00") and its minutes from the sun (`startOffset`, −180…180).
 *
 * @typedef {{sceneId: string, days: ReadonlyArray<boolean>, start: string, startAt: string,
 *            startOffset: number, end: string, endAt: string, endOffset: number,
 *            priority: string, name: string, expected?: object}} ProgramDraft
 */
export const NEW_PROGRAM_DRAFT = Object.freeze({
  sceneId: "",
  days: EVERY_DAY,
  start: "",
  startAt: "clock",
  startOffset: 0,
  end: "",
  endAt: "clock",
  endOffset: 0,
  priority: "normal",
  name: "",
});

/**
 * The Schedule flow's seed: a new Program draft, its Scene prefilled with the Scene the
 * operator last saved or picked (the shell's `recentScene`) while it is still stored, as
 * showNowModel.js `seedShowNow` does; an edit key seeds from the stored Program.
 *
 * @param {string|null} recentSceneId
 * @param {Record<string, object>} definitions the stored Scenes
 * @param {Record<string, object>} [programs] the stored Programs by id
 * @returns {(key: string) => ProgramDraft|null}
 */
export function seedSchedule(recentSceneId, definitions, programs = {}) {
  const stored = recentSceneId !== null && definitions[recentSceneId] !== undefined;
  const draft = stored ? { ...NEW_PROGRAM_DRAFT, sceneId: recentSceneId } : NEW_PROGRAM_DRAFT;
  return (key) => {
    const id = editedId(key);
    if (id === null) return draft;
    const program = programs[id];
    return program === undefined ? null : programEditDraft(program);
  };
}

/** A When's start or end as the draft's three fields, under `key` ("start" or "end"). */
function timeFields(key, time) {
  return {
    [key]: time.at === "clock" ? time.time : "",
    [`${key}At`]: time.at,
    [`${key}Offset`]: time.at === "clock" ? 0 : time.offset_minutes ?? 0,
  };
}

/**
 * The editable draft of a stored Program, keeping the whole Program as `expected` (the save's
 * precondition). Null for a Program with no When.
 */
export function programEditDraft(program) {
  const { when } = program;
  if (when?.start === undefined || when?.end === undefined) return null;
  return {
    sceneId: program.scene_id,
    days: DAY_KEYS.map((day) => when.days.includes(day)),
    ...timeFields("start", when.start),
    ...timeFields("end", when.end),
    priority: program.priority,
    name: program.name,
    expected: program,
  };
}

const CLOCK = /^([01][0-9]|2[0-3]):[0-5][0-9]$/;

/**
 * A draft's problems before it is sent: each field the operator must fill. Central checks the
 * rest and answers a code (`REFUSAL_FIELDS` routes it to its field).
 *
 * @param {ProgramDraft} draft
 * @returns {{field: string, message: string}[]}
 */
export function programDraftProblems(draft) {
  const problems = [];
  if (draft.sceneId === "") problems.push({ field: "scene", message: "Choose a Scene." });
  if (!draft.days.some(Boolean)) problems.push({ field: "days", message: "Tick at least one day." });
  if (draft.startAt === "clock" && !CLOCK.test(draft.start)) problems.push({ field: "start", message: "Enter when it starts." });
  if (draft.endAt === "clock" && !CLOCK.test(draft.end)) problems.push({ field: "end", message: "Enter when it ends." });
  if (draft.name.trim() === "") problems.push({ field: "name", message: "Enter a name." });
  else if (draft.name.trim().length > 80) problems.push({ field: "name", message: "At most 80 characters." });
  return problems;
}

/** Central's refusal codes (central/runtime.py `ProgramRefused`) by the field they concern. */
export const REFUSAL_FIELDS = Object.freeze({
  "no-days": { field: "days", message: "Tick at least one day." },
  "unknown-scene": { field: "scene", message: "That Scene no longer exists; choose another." },
  "needs-place": { field: "start", message: "Sunrise and sunset need the house's location." },
});

/** The draft's start or end (`key`) as a When's TimeOfDay (central/house_time/model.py). */
function timeOf(draft, key) {
  const at = draft[`${key}At`];
  return at === "clock" ? { at, time: draft[key] } : { at, offset_minutes: draft[`${key}Offset`] };
}

/**
 * The body Central stores (central/runtime.py `ProgramDraft`): a name, a Scene, a weekly When
 * on the house clock (clock times, or sunrise or sunset plus or minus minutes), and a priority.
 *
 * @param {ProgramDraft} draft
 * @returns {{name: string, scene_id: string, when: object, priority: string}}
 */
export function buildProgram(draft) {
  return {
    name: draft.name.trim(),
    scene_id: draft.sceneId,
    when: {
      days: DAY_KEYS.filter((_day, index) => draft.days[index]),
      start: timeOf(draft, "start"),
      end: timeOf(draft, "end"),
    },
    priority: draft.priority,
  };
}
