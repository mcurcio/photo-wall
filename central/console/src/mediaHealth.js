import { ageAt, formatAge, frameHealth, gigabytes } from "./health.js";
import { LIVE_PHASES, rankedContributions, toTarget } from "./join.js";
import { clockTime, cycleWording, runScene } from "./showState.js";
import { sourceName } from "./sourceNames.js";

/**
 * The media pipeline in words (pass 2 slice 3 §14). Pure reads of the served
 * `/v1/operator/media` payload (`health`, `sources`) and the runtime, aged on
 * Central's clock (the inventory's `read_at`). Everything here is Central's:
 * the worker fetches from the photo library and prepares media, and Players
 * receive it only from Central (requirements: Players are library-unaware).
 * Nothing here says a panel shows anything (R2).
 *
 * @typedef {"ok"|"todo"|"alarm"} Severity
 * @typedef {{state: string, severity: Severity, label: string}} Classified
 */

// The worker's maintenance runs every 5 min (media/task_queue.py MAINTENANCE_CRON)
// and each run records that it checked in (media_settings.worker_seen).
export const WORKER_CHECK_IN_SECONDS = 300;
// Sources are refreshed every 30 s (media/task_queue.py REFRESH_CRON;
// central/media_repository.py StoreLimits.refresh_seconds sets next_refresh).
export const SOURCE_REFRESH_SECONDS = 30;
// One refresh may run this long (media/worker.py WorkerLimits.refresh_seconds).
export const REFRESH_RUN_SECONDS = 65;
// Past these, a missed beat is not jitter: two intervals plus slack. A pytest
// pins the three constants above to the worker's schedule.
export const WORKER_QUIET_AFTER = 2 * WORKER_CHECK_IN_SECONDS + 60;
export const REFRESH_OVERDUE_AFTER = 2 * SOURCE_REFRESH_SECONDS + REFRESH_RUN_SECONDS;

/** Central's clock for media ages: the inventory's read time. */
export function mediaNow(snapshot) {
  return snapshot?.inventory?.read_at;
}

/** "4 min" for a known age; fails closed to "an unknown time" (no read time). */
function age(seconds) {
  return Number.isFinite(seconds) ? formatAge(Math.max(0, seconds)) : "an unknown time";
}

/** A code as words: "storage_pressure" → "storage pressure". */
export function codeWords(code) {
  return String(code).replaceAll("_", " ");
}

const WORKER_ERRORS = { storage_pressure: "storage is full" };

/**
 * The worker's jobs of its current recipe (central/media_repository.py
 * `health`) and cache: "preparing 3 · waiting 12 · failed 2 · failed, retry
 * pending 1 · cache 4.1 of 8 GB". Preparing is running or publishing; waiting
 * is queued (central/migrations/005_media_jobs.sql states). A job waiting to
 * retry reads as failed, as planning treats it until its retry is due (the
 * catalog hydrates it as a preparation failure); that part shows only when
 * there is one.
 */
export function workerLoad(health) {
  const jobs = Object.fromEntries((health?.jobs ?? []).map((row) => [row.state, row.count]));
  const count = (...states) => states.reduce((sum, state) => sum + (jobs[state] ?? 0), 0);
  const retrying = count("retry") > 0 ? ` · failed, retry pending ${count("retry")}` : "";
  return (
    `preparing ${count("running", "publishing")} · waiting ${count("queued")} · ` +
    `failed ${count("failed")}${retrying} · cache ${gigabytes(health?.accounted_bytes)} of ` +
    `${gigabytes(health?.max_bytes)} GB`
  );
}

/**
 * The worker's state, first match (§14): never checked in, reported an error,
 * quiet past {@link WORKER_QUIET_AFTER}, ok.
 *
 * @param {object|null} health `/v1/operator/media` `health`
 * @param {number} now Central's clock
 * @param {boolean} includeFilters include the Source's criteria in the label (default true)
 * @returns {Classified|null} null when the media read carried no health
 */
export function workerState(health, now) {
  if (health == null) {
    return null;
  }
  if (health.worker_seen == null) {
    return { state: "never", severity: "alarm", label: "never checked in" };
  }
  if (health.worker_error) {
    const words = WORKER_ERRORS[health.worker_error] ?? codeWords(health.worker_error);
    return { state: "error", severity: "alarm", label: `reported: ${words}` };
  }
  const since = ageAt(now, health.worker_seen);
  if (!(since <= WORKER_QUIET_AFTER)) {
    return { state: "quiet", severity: "alarm", label: `quiet for ${age(since)}` };
  }
  return {
    state: "ok",
    severity: "ok",
    label: `checked in ${age(since)} ago · ${workerLoad(health)}`,
  };
}

const SOURCE_FAILURES = {
  unavailable: "Library unreachable",
  permission: "Library refused access",
  incompatible: "Library unsupported",
};

/** A local date, "3 Mar 2025". */
function day(epochSeconds) {
  return new Date(epochSeconds * 1000).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

/** Whether an instant is local midnight on 1 January. */
function newYear(epochSeconds) {
  const date = new Date(epochSeconds * 1000);
  return date.getMonth() === 0 && date.getDate() === 1 && date.getHours() === 0 &&
    date.getMinutes() === 0 && date.getSeconds() === 0;
}

/**
 * A Source's filters in words (§7, §14): its kinds, favourites and capture
 * window ("taken until" is exclusive). "photos only · only favourites ·
 * taken 2024". Empty when it takes everything.
 *
 * @param {object|null} spec the stored SourceSpec
 * @returns {string[]}
 */
export function sourceFilters(spec) {
  const filters = [];
  const kinds = spec?.media_types ?? ["image", "video"];
  if (kinds.length === 1) {
    filters.push(kinds[0] === "image" ? "photos only" : "videos only");
  }
  if (spec?.favorites === true) {
    filters.push("only favourites");
  } else if (spec?.favorites === false) {
    filters.push("no favourites");
  }
  const from = spec?.captured_from ?? null;
  const until = spec?.captured_until ?? null;
  if (from !== null && until !== null) {
    const year = new Date(from * 1000).getFullYear();
    const wholeYear =
      newYear(from) && newYear(until) && new Date(until * 1000).getFullYear() === year + 1;
    // "Taken until" is exclusive: the last day named is the one holding its
    // last included second, so a day made 23 or 25 h long by DST is still whole.
    filters.push(wholeYear ? `taken ${year}` : `taken ${day(from)} to ${day(until - 1)}`);
  } else if (from !== null) {
    filters.push(`taken from ${day(from)}`);
  } else if (until !== null) {
    filters.push(`taken before ${day(until)}`);
  }
  return filters;
}

/**
 * One Source's state, first match (§14): awaiting its first refresh
 * (`next_refresh = 0`, 005_media_jobs.sql), failing, overdue past
 * {@link REFRESH_OVERDUE_AFTER}, nothing valid in its last refresh, ok. "Valid"
 * is the refresh's own count (`counts.valid`): items it found and accepted,
 * not items ready for any frame. The label carries the Source's filters.
 *
 * @param {object} source a `/v1/operator/media` `sources` row
 * @param {number} now Central's clock
 * @returns {Classified}
 */
export function sourceState(source, now, includeFilters = true) {
  const filters = sourceFilters(source.spec);
  const said = (state, severity, label) => ({
    state,
    severity,
    label: includeFilters ? [label, ...filters].join(" · ") : label,
  });
  // A newly configured Source starts as unavailable before any refresh attempt.
  // A failed periodic refresh can leave the explicit request revision at zero, so
  // an absent next_refresh is the reliable signal that no attempt has run yet.
  if (!source.next_refresh && Number(source.refresh_completed_revision ?? 0) === 0 &&
      !source.diagnostics?.length) {
    return said("never-refreshed", "todo", "Awaiting refresh");
  }
  if (source.status !== "ok") {
    const good = source.last_success == null
      ? "never refreshed successfully"
      : `last good ${age(ageAt(now, source.last_success))} ago`;
    const failure = SOURCE_FAILURES[source.status] ?? `Library ${codeWords(source.status)}`;
    return said("failing", "alarm", `${failure} · ${good}`);
  }
  if (!source.next_refresh) {
    return said("never-refreshed", "todo", "Awaiting refresh");
  }
  const late = ageAt(now, source.next_refresh);
  if (!(late <= REFRESH_OVERDUE_AFTER)) {
    return said("overdue", "alarm", `Refresh overdue by ${age(late)}`);
  }
  const valid = Number(source.counts?.valid ?? 0);
  const pending = Number(source.counts?.pending ?? 0);
  const rejected = Number(source.counts?.rejected ?? 0);
  const partialCount = Math.max(0, pending) + Math.max(0, rejected);
  if (!(valid > 0)) {
    return said("empty", "todo", "nothing valid in the last refresh");
  }
  const qualifier = partialCount > 0
    ? ` · ${partialCount} item${partialCount === 1 ? "" : "s"} pending or rejected`
    : "";
  return said("ok", "ok",
    `refreshed ${age(ageAt(now, source.last_success))} ago · ${valid} valid in the last refresh${qualifier}`);
}

// --- One candidate's standing for a frame, as Central serves it.

// The words for each served standing (central/planner.py `candidate_standing`).
const STANDING_WORDS = {
  usable: "ready",
  preparing: "preparing",
  failed_to_prepare: "failed to prepare",
  no_compatible_variant: "no compatible version",
};

/**
 * Chooser labels, in the candidates' order: "Photo 108×192 · taken 3 Mar 2025
 * 14:02 · ready", with " (2)" added only to a label that repeats an earlier one.
 * The readiness is the candidate's served `standing` for the chooser's frame.
 *
 * @param {Array<object>} candidates candidates read for one frame
 * @returns {string[]}
 */
export function candidateLabels(candidates) {
  const seen = new Map();
  return candidates.map((candidate) => {
    const kind = candidate.kind === "video" ? "Video" : "Photo";
    const label =
      `${kind} ${candidate.original_width}×${candidate.original_height} · taken ` +
      `${day(candidate.captured_at)} ${clockTime(candidate.captured_at)} · ` +
      (STANDING_WORDS[candidate.standing] ?? codeWords(candidate.standing));
    const repeat = (seen.get(label) ?? 0) + 1;
    seen.set(label, repeat);
    return repeat === 1 ? label : `${label} (${repeat})`;
  });
}

/**
 * "Check this frame" (§14 step 5): a tally of the served standings of the
 * frame's items across the Scene's Sources. A Source whose last refresh
 * failed is left out, as planning leaves it out (central/planner.py `_pool`),
 * and an item several Sources share counts once: its standing is the same
 * from each, since Central reads it per item, not per Source.
 *
 * @param {Array<{status: string, candidates: Array<object>}>} reads one
 *   `readCandidates` result per Source
 * @returns {Record<"usable"|"preparing"|"failed_to_prepare"|"no_compatible_variant", number>}
 */
export function checkCounts(reads) {
  const standings = new Map();
  for (const read of reads.filter((entry) => entry.status === "ok")) {
    for (const candidate of read.candidates) {
      standings.set(candidate.asset_id, candidate.standing);
    }
  }
  const counts = { usable: 0, preparing: 0, failed_to_prepare: 0, no_compatible_variant: 0 };
  for (const standing of standings.values()) {
    counts[standing] += 1;
  }
  return counts;
}

/** The check's result as a step. */
function checkStep(frameId, check) {
  if (check == null) {
    return {
      state: "check",
      text: "Reads the Sources' current items for this frame's shape.",
    };
  }
  if (check.error != null) {
    return { state: "info", text: `Could not check: ${check.error}.` };
  }
  const {
    usable,
    preparing,
    failed_to_prepare: failed,
    no_compatible_variant: incompatible,
  } = check.counts;
  const rest = [
    preparing > 0 ? `${preparing} still preparing` : null,
    failed > 0 ? `${failed} failed to prepare` : null,
    incompatible > 0 ? `${incompatible} with no compatible version` : null,
  ].filter((part) => part !== null);
  if (usable + rest.length === 0) {
    return { state: "stop", text: `No item in the Source fits ${frameId}'s shape.` };
  }
  if (usable === 0) {
    return { state: "stop", text: `Nothing usable yet: ${rest.join(" · ")}.` };
  }
  const note = preparing + incompatible > 0
    ? " Central picks one per cycle; a cycle that lands on an item not ready plans the next " +
      "layer down."
    : "";
  return { state: "ok", text: `${[`${usable} usable`, ...rest].join(" · ")}.${note}` };
}

/**
 * The latest served root Run that ended with this frame among its participants.
 */
function lastEnded(runtime, frameId) {
  const target = toTarget(frameId);
  return (runtime?.current?.runs ?? [])
    .filter((run) => run.parent_id === null && !LIVE_PHASES.has(run.phase) && run.ended_at != null)
    .filter((run) => run.participants.includes(target))
    .sort((a, b) => b.ended_at - a.ended_at)[0] ?? null;
}

/**
 * @typedef {{title: string, state: "ok"|"stop"|"info"|"skip"|"check", text: string,
 *            stops?: boolean}} Step
 */

/**
 * "Why nothing new on <frame>?" (§14): the chain from intent to equipment,
 * each step a served fact. The first step that is not ok is marked `stops`.
 * `check` is the result of "Check this frame" (null until asked).
 *
 * @param {object|null} snapshot
 * @param {string} frameId
 * @param {{counts?: object, error?: string}|null} [check]
 * @returns {Step[]}
 */
export function whyNothingNew(snapshot, frameId, check = null) {
  const runtime = snapshot?.runtime;
  const now = mediaNow(snapshot);
  const [winner] = rankedContributions(runtime, frameId);
  const ended = winner === undefined ? lastEnded(runtime, frameId) : null;
  const skip = (title) => ({ title, state: "skip", text: "Not reached." });
  const steps = [];

  if (winner !== undefined) {
    steps.push({
      title: "Intended?",
      state: "ok",
      text: `Central's plan puts ${winner.scene_id} (priority ${winner.priority}) here.`,
    });
    steps.push({ title: "Run ended?", state: "ok", text: "No: its Run is still going." });
  } else {
    steps.push({
      title: "Intended?",
      state: ended === null ? "stop" : "info",
      text: `No Scene is intended for ${frameId} now.`,
    });
    steps.push(ended === null ? skip("Run ended?") : endedStep(snapshot, ended, frameId));
  }

  const live = winner !== undefined && winner.kind === "media" && winner.asset_refs.length === 0;
  if (winner === undefined) {
    steps.push(skip("Authored?"));
  } else if (winner.kind !== "media") {
    steps.push({ title: "Authored?", state: "stop", text: "It shows black here by design." });
  } else if (!live) {
    steps.push({
      title: "Authored?",
      state: "stop",
      text: "Fixed, hand-picked media; new photos never appear by design.",
    });
  } else {
    steps.push({ title: "Authored?", state: "ok", text: `No: live from ${winner.source_refs.map(sourceName).join(", ")}.` });
  }

  if (live) {
    const sources = snapshot?.media?.sources ?? [];
    const read = winner.source_refs.map((ref) => {
      const source = sources.find((candidate) => candidate.source_ref === ref);
      return source === undefined
        ? { ok: false, saved: true, text: `${sourceName(ref)}: this Run uses saved Source settings; their current status is not shown here` }
        : (({ severity, label }) => ({ ok: severity === "ok", text: `${sourceName(source)}: ${label}` }))(
          sourceState(source, now),
        );
    });
    steps.push({
      title: "The Source",
      state: read.some((entry) => entry.ok) ? "ok" : read.some((entry) => entry.saved) ? "info" : "stop",
      text: `${read.map((entry) => entry.text).join("; ")}.`,
    });
    steps.push({ title: "Check this frame", ...checkStep(frameId, check) });
  } else {
    steps.push(skip("The Source"), skip("Check this frame"));
  }

  const worker = workerState(snapshot?.media?.health, now);
  steps.push({
    title: "The worker",
    state: worker?.severity === "ok" ? "ok" : "stop",
    text: worker === null ? "Its state was not served." : `The media worker ${worker.label}.`,
  });
  const health = frameHealth(snapshot, frameId);
  steps.push({
    title: "Frame health",
    state: health?.severity === "ok" ? "ok" : "stop",
    text: health === null ? `${frameId} is not in the inventory.` : health.label,
  });

  const first = steps.findIndex((step) => step.state === "stop");
  return steps.map((step, index) => (index === first ? { ...step, stops: true } : step));
}

/** Step 2 for a frame nothing targets now: the Run that last did (§14). */
function endedStep(snapshot, run, frameId) {
  const scene = runScene(snapshot, run);
  if (run.phase === "cancelled") {
    return {
      title: "Run ended?",
      state: "stop",
      text: `${run.scene_id}'s Run was cancelled at ${clockTime(run.ended_at)}.`,
    };
  }
  const once = cycleWording(scene) !== null ? " after one cycle" : "";
  const retains = (scene?.contributions ?? []).some(
    (entry) => entry.target === toTarget(frameId) && entry.retain_on_expiry,
  );
  const still = retains
    ? "; if its last item was a photo, the frame keeps that still (a video is not kept)"
    : "";
  return {
    title: "Run ended?",
    state: "stop",
    text: `${run.scene_id}'s Run ended at ${clockTime(run.ended_at)}${once}${still}.`,
  };
}
