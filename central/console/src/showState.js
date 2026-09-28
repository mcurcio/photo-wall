import { ageAt, formatAge } from "./health.js";
import { LIVE_PHASES } from "./join.js";

/**
 * Program and Run display states (pass 2 slice 3 §9). Pure reads of the served
 * `/v1/operator/runtime` payload: every sentence restates a served fact, aged
 * on Central's clock (`current.now`), and states its limit. "Ran" and
 * "running" describe Central's plan, never what a panel showed (R2); times come
 * from the Run (`started_at`, `ended_at`), never from the Program's window.
 *
 * @typedef {"ok"|"todo"|"alarm"} Severity
 * @typedef {{state: string, label: string, severity: Severity, hint: string|null}} ProgramState
 */

const FRAME = "frame:";

/** The frame id of a `frame:<id>` target. */
export function frameOf(target) {
  return target.slice(FRAME.length);
}

/** Every target a stored Scene reaches (central/runtime.py `Scene.participants`). */
export function sceneParticipants(scene) {
  const own = [...(scene?.contributions ?? []), ...(scene?.outro_contributions ?? [])].map(
    (contribution) => contribution.target,
  );
  const children = (scene?.children ?? []).flatMap((child) => sceneParticipants(child.scene));
  return [...new Set([...own, ...children])];
}

/** The targets a stored Scene protects (central/runtime.py `Scene.protected_frames`). */
export function sceneProtectedFrames(scene) {
  if (scene?.protect_frames) {
    return sceneParticipants(scene).filter((target) => target.startsWith(FRAME));
  }
  return [...new Set((scene?.children ?? []).flatMap((child) => sceneProtectedFrames(child.scene)))];
}

/** The stored Scene a Run started from, while the definition keeps that revision. */
function runScene(snapshot, run) {
  const scene = snapshot?.runtime?.definitions?.[run.scene_id];
  return scene !== undefined && scene.revision === run.scene_revision ? scene : null;
}

/** "plays one 30 s cycle, then ends" for a Scene without `loop`; else null. */
export function cycleWording(scene) {
  return scene != null && scene.loop === false
    ? `plays one ${Number(scene.cycle_seconds)} s cycle, then ends`
    : null;
}

const pad = (value) => String(value).padStart(2, "0");

/** A local clock time, "18:00", with seconds only when they are not zero. */
export function clockTime(epochSeconds) {
  const date = new Date(epochSeconds * 1000);
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  return date.getSeconds() === 0 ? time : `${time}:${pad(date.getSeconds())}`;
}

/** A Program window in local time: "Tue 2 Mar 18:00–20:00". */
export function windowLabel(program) {
  const day = (epoch) =>
    new Date(epoch * 1000).toLocaleDateString(undefined, {
      weekday: "short",
      day: "numeric",
      month: "short",
    });
  const start = day(program.starts_at);
  const end = day(program.ends_at);
  return start === end
    ? `${start} ${clockTime(program.starts_at)}–${clockTime(program.ends_at)}`
    : `${start} ${clockTime(program.starts_at)} – ${end} ${clockTime(program.ends_at)}`;
}

const list = (targets) => targets.map(frameOf).join(", ");

/**
 * The served root Run that refused a Scene, live at `at` (started at or before
 * it, not ended before it): for `protected_frames`, one whose served
 * `protected_frames` cover the Scene's frames; for `protection_not_visible`, a
 * higher-priority one covering the frames the Scene protects. `frames` is what
 * was covered — the Scene's own frames when no protector is served.
 *
 * @param {object|null} snapshot
 * @param {object|undefined} scene the stored Scene that was refused
 * @param {number} priority its priority
 * @param {"protected_frames"|"protection_not_visible"} reason
 * @param {number} at
 * @returns {{run: object|null, frames: string[]}}
 */
export function protectorOf(snapshot, scene, priority, reason, at) {
  const runtime = snapshot?.runtime;
  const protectedBy = runtime?.protected_frames ?? {};
  const frames =
    reason === "protected_frames"
      ? sceneParticipants(scene).filter((target) => target.startsWith(FRAME))
      : sceneProtectedFrames(scene);
  for (const run of runtime?.current?.runs ?? []) {
    if (run.parent_id !== null || run.started_at > at || (run.ended_at != null && run.ended_at < at)) {
      continue;
    }
    const cover =
      reason === "protected_frames"
        ? protectedBy[run.run_id] ?? []
        : run.priority > priority
          ? run.participants
          : [];
    const covered = frames.filter((target) => cover.includes(target));
    if (covered.length > 0) {
      return { run, frames: covered };
    }
  }
  return { run: null, frames };
}

const RAN_HINT =
  "Central's plan: if Central was down during the window, it caught up without showing anything.";

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
    const scene = runtime.definitions?.[program.scene_id];
    const found = protectorOf(snapshot, scene, program.priority, outcome.reason, program.starts_at);
    return state("refused", REFUSALS[outcome.reason](list(found.frames), found.run), "alarm");
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

const REFUSALS = {
  protected_frames: (frames, run) =>
    run !== null
      ? `Did not start: ${frames} was protected by the Run of ${run.scene_id}.`
      : `Did not start: ${frames} was protected by another Run, no longer listed.`,
  protection_not_visible: (frames, run) =>
    run !== null
      ? `Did not start: it protects ${frames}, but a higher-priority Run of ${run.scene_id} covered it.`
      : `Did not start: it protects ${frames}, but a higher-priority Run covered it, no longer listed.`,
};

/**
 * @typedef {{run: object, origin: string, started: string, status: string,
 *            finishing: boolean, cycle: string|null, protects: string[],
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
  const row = (run) => ({
    run,
    origin:
      run.parent_id !== null
        ? `part of ${byId.get(run.root_id)?.scene_id ?? "another Run"}`
        : run.program_id != null
          ? `Program ${run.program_id}`
          : "activated directly",
    started: `Started${ago(run.started_at)}`,
    status: status(run),
    finishing: run.phase !== "body" || run.finish_requested_at != null,
    cycle: cycleWording(runScene(snapshot, run)),
    protects: (protectedBy[run.run_id] ?? []).map(frameOf),
    frames: run.participants.filter((target) => target.startsWith(FRAME)).map(frameOf).sort(),
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
