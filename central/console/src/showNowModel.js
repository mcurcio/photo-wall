/**
 * The Show-now flow's shape (flow design §7 J7): its steps, which step asks each field,
 * its one instance and its route, its seed, and the draft rules for the activation
 * key. Pure: no React; tests/test_console_show_now.py drives it under Node.
 *
 * THE DRAFT is `{sceneId, priority, repeat, activationKey}`. `priority` is null while
 * it follows its default (showState.js `coveringPriority` of the Scene's frames, read
 * from the current snapshot), or the operator's own value once changed under Advanced.
 * `repeat` is "ignore" ("Leave it running", the default) or "restart".
 *
 * THE ACTIVATION KEY lives in the draft (slice 3B §11): it is minted with the draft,
 * and every change the operator makes to the form mints a new one ({@link editActivation}),
 * so a changed form is a new activation. Nothing else changes it: a step or section
 * change, a Wall visit, a poll or the sign-in overlay keep it (the flow container
 * never unmounts), and an unknown outcome keeps it, so a retry sends the same key and
 * Central answers it with the stored Admission (central/runtime.py `activate`). A
 * known outcome ends the flow, and the next draft has a new key.
 */

import { newActivationKey } from "./authoring.js";
import { sameValue } from "./flow/draftState.js";
import { flowKeys } from "./flow/instance.js";
import { ORIGIN_REFUSALS } from "./session.js";

/** @typedef {import("./flow/steps.js").Step} Step */
/**
 * @typedef {{sceneId: string, priority: string|number|null, repeat: "ignore"|"restart",
 *            activationKey: string}} ShowNowDraft
 */

const SCENE = Object.freeze({ id: "scene", label: "Scene" });
const REVIEW = Object.freeze({ id: "review", label: "Review" });

/** @type {ReadonlyArray<Step>} */
export const SHOW_STEPS = Object.freeze([SCENE, REVIEW]);

/**
 * Which step asks each field: those of authoring.js `activationProblems`, and "repeat",
 * which Review's "Change" reaches.
 */
export const SHOW_FIELD_STEP = Object.freeze({
  scene: SCENE.id,
  priority: REVIEW.id,
  repeat: REVIEW.id,
});

/** The fields under Review's Advanced (they have a stated default). */
export const SHOW_ADVANCED_FIELDS = Object.freeze(new Set(["priority", "repeat"]));

/** The Show-now flow's one instance, `#/now/show/<step>` (flow/instance.js `flowKeys`). */
export const SHOW_KEYS = flowKeys({
  section: "now",
  newFlow: "show",
  firstStep: { create: SCENE.id },
  describe: { create: "a new activation" },
});

/** The labels of "If it is already running" (slice 3 §11; rule 3 keeps them). */
export const REPEAT_LABELS = Object.freeze({ ignore: "Leave it running", restart: "Restart it" });

/**
 * The Show-now seed: the Scene the operator last saved or picked (the shell's
 * `recentScene`) when it is still stored, the priority on its default, "Leave it
 * running", and a fresh activation key.
 *
 * @param {string|null} recentSceneId
 * @param {Record<string, object>} definitions the stored Scenes
 * @param {() => string} [mint] the key minter (authoring.js `newActivationKey`)
 * @returns {() => ShowNowDraft}
 */
export function seedShowNow(recentSceneId, definitions, mint = newActivationKey) {
  return () => ({
    sceneId: recentSceneId !== null && definitions[recentSceneId] !== undefined ? recentSceneId : "",
    priority: null,
    repeat: "ignore",
    activationKey: mint(),
  });
}

/**
 * The operator's change `changes` as a draft patch (useFlowDraft `patch`): a change to
 * the form mints a new activation key; setting a value to what it already is changes
 * nothing, so the key is kept. A new Scene puts the priority back on its default
 * (the default is the new Scene's frames').
 *
 * @param {Partial<ShowNowDraft>} changes
 * @param {() => string} [mint]
 * @returns {(value: ShowNowDraft) => Partial<ShowNowDraft>|null}
 */
export function editActivation(changes, mint = newActivationKey) {
  return (value) => {
    const changed = Object.keys(changes).some((key) => !sameValue(value[key], changes[key]));
    if (!changed) {
      return null;
    }
    const scene = "sceneId" in changes && changes.sceneId !== value.sceneId ? { priority: null } : {};
    return { ...scene, ...changes, activationKey: mint() };
  };
}

/**
 * How an answer to the activation write (apiWrite's result, or null when the request
 * threw or timed out) ends the flow:
 *
 *  - `unknown`: no answer, or a 5xx; it may have started, so the draft and its key stay
 *    for the retry;
 *  - `known`: Central's Runtime answered it, with an Admission (2xx) or by refusing the
 *    command (409, or 422 `invalid_command`: central/app.py maps the Runtime's
 *    conflicts and ValueErrors so); the flow ends;
 *  - `kept`: refused before it reached the Runtime (the session ended, the page's origin
 *    was refused, the request itself was refused): nothing started, and the draft and
 *    its key stay, with `text` saying why.
 *
 * @param {{ok: boolean, status: number, error: string|null}|null} result
 * @returns {{kind: "unknown"|"known"}|{kind: "kept", text: string}}
 */
export function activationAnswer(result) {
  if (result === null || result.status >= 500) {
    return { kind: "unknown" };
  }
  if (result.ok || result.status === 409 || result.error === "invalid_command") {
    return { kind: "known" };
  }
  if (result.status === 401) {
    return { kind: "kept", text: "Not started: the session ended. Sign in again, then activate." };
  }
  if (result.status === 403 && ORIGIN_REFUSALS.has(result.error)) {
    return {
      kind: "kept",
      text:
        "Not started: Central refused the request from this page. Reload the console " +
        "from the address you signed in at, then activate.",
    };
  }
  return {
    kind: "kept",
    text: `Not started: ${result.error ?? `HTTP ${result.status}`}. Nothing reached Central's Runtime.`,
  };
}

/**
 * The priority the form shows and sends: the operator's own, or the default.
 *
 * @param {ShowNowDraft} value
 * @param {number} covering showState.js `coveringPriority` of the Scene's frames
 * @returns {string|number}
 */
export function shownPriority(value, covering) {
  return value.priority === null ? covering : value.priority;
}
