import { ageAt, formatAge } from "./health.js";
import { frameOf, LIVE_PHASES, toTarget } from "./join.js";
import { sceneFrames } from "./sceneTargets.js";
import { clockTime, windowLabel } from "./timeWords.js";

export { sceneFrames, sceneSourceRefs, sceneHasAuthoredMedia } from "./sceneTargets.js";

/**
 * Program and Run display states (pass 2 slice 3 §9). Pure reads of the served
 * `/v1/operator/runtime` payload: every sentence restates a served fact, aged
 * on Central's clock (`current.now`), and states its limit. "Ran" and
 * "running" describe Central's Runs, never what a panel showed (R2); times come
 * from the Run (`started_at`, `ended_at`), never from the Program's window, and every
 * clock time is zoned (timeWords.js).
 *
 * @typedef {"ok"|"todo"|"alarm"} Severity
 * @typedef {{state: string, label: string, severity: Severity, hint: string|null}} ProgramState
 */

/** The stored Scene a Run started from, while the definition keeps that revision. */
export function runScene(snapshot, run) {
  const scene = snapshot?.runtime?.definitions?.[run.scene_id];
  return scene !== undefined && scene.revision === run.scene_revision ? scene : null;
}

/** "plays one 30 s cycle, then ends" for a Scene without `loop`; else null. */
export function cycleWording(scene) {
  return scene != null && scene.loop === false
    ? `plays one ${Number(scene.cycle_seconds)} s cycle, then ends`
    : null;
}

/**
 * Who started a Run (§34, §35): the one home of a Run's origin, read by the Run cards and by
 * the Frame's `planned` fact (join.js `plannedFor`). A child Run's `program_id` is null
 * (central/runtime.py), so a child names its root's Run; a root names its Program, or says
 * it was started directly. `programListed` is false when the Program is no longer stored
 * (`remove_program` drops it after reconciling), and its words then say so; a removed
 * Program is never linked.
 *
 * @param {object|null|undefined} runtime the served runtime (snapshot.runtime)
 * @param {{run_id: string, root_id: string, parent_id: string|null, program_id: string|null}} run
 * @returns {{kind: "program"|"direct"|"child", words: string, programListed: boolean}}
 */
export function runOrigin(runtime, run) {
  if (run.parent_id != null) {
    const root = (runtime?.current?.runs ?? []).find((candidate) => candidate.run_id === run.root_id);
    return {
      kind: "child",
      words: root === undefined ? "part of another Run" : `part of ${root.scene_id}'s Run`,
      programListed: false,
    };
  }
  if (run.program_id != null) {
    const listed = runtime?.programs?.[run.program_id] !== undefined;
    return {
      kind: "program",
      words: listed ? `Program ${run.program_id}` : `Program ${run.program_id}, since removed`,
      programListed: listed,
    };
  }
  return { kind: "direct", words: "started directly (Show now or the API)", programListed: false };
}

/**
 * The served Run that refused an Admission — Central names it in
 * `blocking_run_id` with the refusal, so the console never re-derives the
 * rule — and the frames that Run protects. `run` is null when the Admission
 * names none or that Run is no longer served (ended over a day ago).
 *
 * @param {object|null} snapshot
 * @param {{blocking_run_id?: string|null}|null} admission
 * @returns {{run: object|null, frames: string}}
 */
export function protectorOf(snapshot, admission) {
  const runtime = snapshot?.runtime;
  const run =
    (runtime?.current?.runs ?? []).find((candidate) => candidate.run_id === admission?.blocking_run_id) ??
    null;
  const frames = run === null ? [] : (runtime.protected_frames?.[run.run_id] ?? []).map(frameOf);
  return { run, frames: frames.join(", ") };
}

const RAN_HINT =
  "Central's Runs: if Central was down during the window, it caught up without showing anything.";

/**
 * One stored Program's display state (§9), first match: details older than a
 * day (no served outcome), upcoming, due (transient), running, ran, cancelled,
 * refused, missed.
 *
 * @param {object|null} snapshot
 * @param {string} programId
 * @returns {ProgramState|null} null when the Program is not stored
 */
export function programState(snapshot, programId) {
  const runtime = snapshot?.runtime;
  const program = runtime?.programs?.[programId];
  if (program === undefined) {
    return null;
  }
  const now = runtime.current?.now;
  const outcomes = runtime.program_outcomes ?? {};
  const state = (name, label, severity = "ok", hint = null) => ({ state: name, label, severity, hint });
  if (!(programId in outcomes)) {
    return state("old", "Ended over a day ago; details older than a day");
  }
  const outcome = outcomes[programId];
  if (outcome == null) {
    return now < program.starts_at
      ? state("upcoming", `Starts in ${formatAge(ageAt(program.starts_at, now))} · ${windowLabel(program)}`)
      : state("due", "Due now");
  }
  if (outcome.status === "admitted") {
    const run = (runtime.current?.runs ?? []).find((candidate) => candidate.run_id === outcome.run_id);
    if (run === undefined) {
      return state("ran", "Ran; details older than a day", "ok", RAN_HINT);
    }
    if (LIVE_PHASES.has(run.phase)) {
      return state("running", `Running since ${clockTime(run.started_at)}`);
    }
    if (run.phase === "cancelled") {
      return state("ran", `Cancelled at ${clockTime(run.ended_at)}`, "ok", RAN_HINT);
    }
    const once = cycleWording(runScene(snapshot, run)) !== null ? " (one cycle, then ended)" : "";
    return state(
      "ran",
      `Ran ${clockTime(run.started_at)}–${clockTime(run.ended_at)}${once}`,
      "ok",
      RAN_HINT,
    );
  }
  if (outcome.status === "rejected" && outcome.reason in REFUSALS) {
    return state("refused", REFUSALS[outcome.reason](protectorOf(snapshot, outcome)), "alarm");
  }
  if (outcome.status === "expired" && outcome.reason === "missed_window") {
    return state(
      "missed",
      "Missed: its window had ended before Central first scheduled it.",
      "todo",
    );
  }
  return state("refused", `Did not start: ${outcome.reason ?? outcome.status}.`, "alarm");
}

/**
 * Whether a stored Program is filed under "Past" (§9, flow design §7 J6): its window has
 * ended on Central's clock and it is neither running (its Run is asked to finish at the
 * window's end and may play on to the end of its cycle and its outro) nor due.
 *
 * @param {object|null} snapshot
 * @param {{program_id: string, ends_at: number}} program
 * @returns {boolean}
 */
export function isPastProgram(snapshot, program) {
  if (!(program.ends_at <= snapshot?.runtime?.current?.now)) {
    return false;
  }
  const state = programState(snapshot, program.program_id)?.state;
  return state !== "running" && state !== "due";
}

const REFUSALS = {
  protected_frames: ({ run, frames }) =>
    run !== null
      ? `Did not start: ${frames} was protected by the Run of ${run.scene_id}.`
      : "Did not start: its frames were protected by another Run, no longer listed.",
  protection_not_visible: ({ run }) =>
    run !== null
      ? `Did not start: it protects frames that a higher-priority Run of ${run.scene_id} covered.`
      : "Did not start: it protects frames that a higher-priority Run covered, no longer listed.",
};

/**
 * @typedef {{run: object, origin: string, started: string, status: string,
 *            finishing: boolean, cycle: string|null, protection: string|null,
 *            frames: string[], children: RunRow[]}} RunRow
 */

/**
 * The served Runs as rows (§9): live roots with their children nested beneath,
 * and the ended roots split into completed and cancelled, latest first.
 *
 * @param {object|null} snapshot
 * @returns {{live: RunRow[], completed: RunRow[], cancelled: RunRow[]}}
 */
export function runRows(snapshot) {
  const runtime = snapshot?.runtime;
  const now = runtime?.current?.now;
  const runs = runtime?.current?.runs ?? [];
  const byId = new Map(runs.map((run) => [run.run_id, run]));
  const protectedBy = runtime?.protected_frames ?? {};
  const ago = (at) => {
    const age = ageAt(now, at);
    return Number.isNaN(age) ? "" : ` ${formatAge(Math.max(0, age))} ago`;
  };
  const status = (run) => {
    if (run.phase === "outro") {
      return "Ending (outro)";
    }
    if (run.phase === "body") {
      return run.finish_requested_at != null
        ? `Finishing: requested${ago(run.finish_requested_at)}`
        : "Running";
    }
    return `${run.phase === "cancelled" ? "Cancelled" : "Completed"} at ${clockTime(run.ended_at)}`;
  };
  const protection = (run) => {
    const frames = (protectedBy[run.run_id] ?? []).map(frameOf);
    if (frames.length === 0) {
      return null;
    }
    return `${LIVE_PHASES.has(run.phase) ? "protects" : "protected"} ${frames.join(", ")}`;
  };
  const row = (run) => ({
    run,
    origin: runOrigin(runtime, run).words,
    started: `Started${ago(run.started_at)}`,
    status: status(run),
    finishing: run.phase !== "body" || run.finish_requested_at != null,
    cycle: cycleWording(runScene(snapshot, run)),
    protection: protection(run),
    frames: run.participants.map(frameOf).filter((frameId) => frameId !== null).sort(),
    children: run.children.map((id) => byId.get(id)).filter(Boolean).map(row),
  });
  const roots = runs.filter((run) => run.parent_id === null);
  const ended = (phase) =>
    roots
      .filter((run) => run.phase === phase)
      .sort((a, b) => b.ended_at - a.ended_at)
      .map(row);
  return {
    live: roots.filter((run) => LIVE_PHASES.has(run.phase)).map(row),
    completed: ended("completed"),
    cancelled: ended("cancelled"),
  };
}

/**
 * The frames a stored Scene protects (central/runtime.py `Scene.protected_frames`): all
 * of its frames when it protects them (`protect_frames`), otherwise those its child
 * Scenes protect, sorted.
 *
 * @param {object|null|undefined} scene a served Scene definition
 * @returns {string[]}
 */
export function sceneProtectedFrames(scene) {
  if (scene?.protect_frames === true) {
    return sceneFrames(scene);
  }
  const frames = new Set((scene?.children ?? []).flatMap((child) => sceneProtectedFrames(child.scene)));
  return [...frames].sort();
}

/**
 * The live root Runs covering any of `frameIds`, each with the frames of `frameIds` it
 * covers, highest priority first (at equal priority the later admission first: the
 * served order is admission order). Only roots: a child Run carries its root's
 * priority and root order, and a root's `participants` include its children's targets
 * (central/runtime.py `_admit`, `Scene.participants`), so roots decide who is on top.
 *
 * @param {object|null} snapshot
 * @param {ReadonlyArray<string>} frameIds
 * @returns {{run: object, frames: string[]}[]}
 */
export function coveringRuns(snapshot, frameIds) {
  const runs = snapshot?.runtime?.current?.runs ?? [];
  return runs
    .map((run, order) => ({
      run,
      order,
      frames: frameIds.filter((frameId) => run.participants.includes(toTarget(frameId))),
    }))
    .filter(({ run, frames }) => run.parent_id === null && LIVE_PHASES.has(run.phase) && frames.length > 0)
    .sort((a, b) => b.run.priority - a.run.priority || b.order - a.order)
    .map(({ run, frames }) => ({ run, frames }));
}

/**
 * The priority a new activation on `frameIds` needs to show on top (flow design §6
 * frozen surface, §7 J7): the highest priority among the live root Runs covering any
 * of the frames; 0 when none does.
 *
 * Max, not max + 1: precedence is `(priority, root_order, admission_order)`
 * (central/runtime.py `Intent.precedence`), the visible winner needs a strictly
 * greater tuple (`_view`), and a new root's `root_order` is the admission sequence,
 * which rises with every admission (`_admit`). At equal priority the later admission
 * wins, so the highest covering priority already puts the new Run on top.
 *
 * @param {object|null} snapshot
 * @param {ReadonlyArray<string>} frameIds
 * @returns {number}
 */
export function coveringPriority(snapshot, frameIds) {
  const [top] = coveringRuns(snapshot, frameIds);
  return top === undefined ? 0 : top.run.priority;
}

/**
 * The live root Runs covering any of `frameIds` whose Scene protects some of them, each
 * with those frames, highest first: Central refuses an activation there at ANY priority
 * (central/runtime.py `_protected_conflict`, `protected_frames`; the console never sends
 * `force`). The served `protected_frames` map says what each Run protects (the Scene it
 * started with, not the one stored now).
 *
 * @param {object|null} snapshot
 * @param {ReadonlyArray<string>} frameIds
 * @returns {{run: object, frames: string[]}[]}
 */
export function protectingRuns(snapshot, frameIds) {
  const served = snapshot?.runtime?.protected_frames ?? {};
  return coveringRuns(snapshot, frameIds)
    .map(({ run, frames }) => ({
      run,
      frames: frames.filter((frameId) => (served[run.run_id] ?? []).includes(toTarget(frameId))),
    }))
    .filter(({ frames }) => frames.length > 0);
}

/**
 * What happens to an activation on `frameIds` at `priority` (§7 J7), or null when it
 * shows on top as asked.
 *
 * A live Run whose Scene protects any of the frames refuses it at any priority
 * ({@link protectingRuns}): "Central will refuse this at any priority: a is protected by
 * the Run of X." Otherwise the live root Runs of a strictly higher priority covering its
 * frames, highest first, decide:
 *
 * A Scene that protects frames (`protectedFrames`, {@link sceneProtectedFrames}) is
 * refused when such a Run covers any of them (central/runtime.py `_protected_conflict`,
 * `protection_not_visible`): "At priority P Central will refuse this: it protects a,
 * which the Run of X (priority Q) covers. Use priority at least Q." Otherwise it stays
 * underneath them: "At priority P this stays underneath the Run of X (priority Q) on a,
 * b."
 *
 * @param {object|null} snapshot
 * @param {ReadonlyArray<string>} frameIds
 * @param {number} priority
 * @param {ReadonlyArray<string>} [protectedFrames] the frames the Scene protects
 * @returns {string|null}
 */
export function underneathSentence(snapshot, frameIds, priority, protectedFrames = []) {
  const protecting = protectingRuns(snapshot, frameIds);
  if (protecting.length > 0) {
    const parts = protecting.map(
      ({ run, frames }) => `${frames.join(", ")} ${frames.length === 1 ? "is" : "are"} protected by the Run of ${run.scene_id}`,
    );
    return `Central will refuse this at any priority: ${parts.join("; ")}.`;
  }
  const above = coveringRuns(snapshot, frameIds).filter(({ run }) => run.priority > priority);
  if (above.length === 0) {
    return null;
  }
  const refusing = above
    .map(({ run, frames }) => ({ run, frames: frames.filter((frameId) => protectedFrames.includes(frameId)) }))
    .filter(({ frames }) => frames.length > 0);
  if (refusing.length > 0) {
    const covers = refusing.map(
      ({ run, frames }) =>
        `${frames.join(", ")}, which the Run of ${run.scene_id} (priority ${run.priority}) covers`,
    );
    return (
      `At priority ${priority} Central will refuse this: it protects ${covers.join(", and ")}. ` +
      `Use priority at least ${refusing[0].run.priority}.`
    );
  }
  const parts = above.map(
    ({ run, frames }) => `the Run of ${run.scene_id} (priority ${run.priority}) on ${frames.join(", ")}`,
  );
  return `At priority ${priority} this stays underneath ${parts.join("; and ")}.`;
}
