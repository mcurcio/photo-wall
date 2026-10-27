/**
 * What needs the operator's attention, pure (no React): the rows the attention strip and the
 * Needs attention page list (AttentionList.jsx renders them), their summary, and the
 * planning rows. tests/test_console_flow.py runs it under Node.
 */
import { wallAttention } from "./health.js";
import { hostIncidents, incidentSeverity } from "./hostHealth.js";
import { formatRoute } from "./routes.js";
import { sinceTime } from "./timeWords.js";

/** "1 frame", "3 frames": the scheduler row counts silent frames this way. */
export function frames(count) {
  return `${count} ${count === 1 ? "frame" : "frames"}`;
}

/** The Scene's page, else the Scenes list for an id the routes do not take. */
function sceneHref(sceneId) {
  try {
    return formatRoute({ section: "scenes", id: sceneId, flow: "edit", step: "review" });
  } catch {
    return formatRoute({ section: "scenes" });
  }
}

/**
 * What each scheduler step's failure means to the operator, in the order the rows list them
 * (central/scheduler.py: one facet per step). Planning and the Runtime share one row.
 */
const STEP_WORDS = [
  [["planning", "runtime"], "planning", "Planning is failing"],
  [["settle"], "settle", "Plans are not being committed"],
  [["reconcile"], "reconcile", "Players are not being brought up to date"],
  [["measurement"], "measurement", "Photos are not being measured"],
];

/** Why the last planning pass cut the plan short (`plan_shortened`'s reason), in plain words. */
const SHORTENED_WORDS = {
  runtime_events: "too many schedule changes in the hour ahead",
  assignments: "too many showings in the next minutes",
  slots: "too many photo turns in the hour ahead",
};

/**
 * The Central rows: one row per failing scheduler step ("Planning is failing since Mon 14:03",
 * timeWords.js `sinceTime`, design language §7), whatever the Frames report; one row per Scene
 * the last planning pass refused (`snapshot.planning`, code "intent_refused"), linking to the
 * Scene; one when the pass cut the plan short (`plan_shortened`: Frames may go dark past the
 * cut); and one when it counted issues it could not keep.
 *
 * @param {object} snapshot
 * @param {{steps?: Record<string, {status: string, since: number|null}>}|null} central
 * @returns {AttentionRow[]}
 */
export function planningRows(snapshot, central) {
  const steps = central?.steps ?? {};
  const failingRows = STEP_WORDS.flatMap(([names, key, words]) => {
    const failing = names.map((name) => steps[name]).filter((step) => step?.status === "failing");
    if (failing.length === 0) return [];
    const since = Math.min(...failing.map((step) => step.since ?? Infinity));
    return [{
      key,
      text: Number.isFinite(since) ? `${words} since ${sinceTime(since, snapshot?.readAt)}` : words,
    }];
  });
  const planning = snapshot?.planning ?? {};
  const refused = new Map();
  const shortened = new Set();
  for (const issue of planning.issues ?? []) {
    if (issue.code === "intent_refused" && issue.scene_id && !refused.has(issue.scene_id)) {
      refused.set(issue.scene_id, issue);
    } else if (issue.code === "plan_shortened") {
      shortened.add(SHORTENED_WORDS[issue.reason] ?? "a planning limit");
    }
  }
  return [
    ...failingRows,
    ...[...refused.values()].map((issue) => ({
      key: `scene:${issue.scene_id}`,
      href: sceneHref(issue.scene_id),
      text: `Scene ${issue.scene_id} could not be planned: ${issue.reason ?? "refused"}`,
    })),
    ...(shortened.size > 0
      ? [{
          key: "plan_shortened",
          text: `Plans are cut short (${[...shortened].join("; ")}); Frames may go dark past the cut`,
        }]
      : []),
    ...(planning.dropped > 0
      ? [{ key: "planning_dropped", text: `${planning.dropped} more planning issues were not kept` }]
      : []),
  ];
}

const counted = (count, noun) => `${count} ${noun}${count === 1 ? "" : "s"}`;

/**
 * The strip's one summary (console DDD §61): incidents only, Frames then Players ("2 Frames ·
 * 1 Player need attention", "1 Player needs attention"), else "No Frame or Player needs
 * attention", with the awaiting count. While the fleet host read has failed or not yet loaded
 * it ends " · host health not read", so missing host rows never read as health.
 *
 * The planning rows (`central`) lead it as "Central".
 *
 * @param {{alarms: Array<object>, players: number, awaiting: number, hostsUnread: boolean,
 *          central?: number}} view
 * @returns {string}
 */
export function attentionSummary({ alarms, players, awaiting, hostsUnread, central = 0 }) {
  const parts = [
    ...(central > 0 ? ["Central"] : []),
    ...(alarms.length > 0 ? [counted(alarms.length, "Frame")] : []),
    ...(players > 0 ? [counted(players, "Player")] : []),
  ];
  const single = parts.length === 1 && (central > 0 || alarms.length + players === 1);
  const text = parts.length === 0
    ? `No Frame or Player needs attention${awaiting > 0 ? ` · ${awaiting} awaiting a first report` : ""}`
    : `${parts.join(" · ")} ${single ? "needs" : "need"} attention`;
  return hostsUnread ? `${text} · host health not read` : text;
}

/**
 * @typedef {{key: string, text: string, severity?: string, frameId?: string, href?: string,
 *            health?: import("./health.js").FrameHealth, deviceId?: string, name?: string,
 *            frames?: string[], hardwareHref?: string}} AttentionRow
 *   A Player row (`deviceId` set) is a host incident of a Bound Player (hostHealth.js
 *   `hostIncidents`): it names the Player and its Frames, and links to both.
 */

/**
 * What needs the operator, from the one classifier (health.js `wallAttention`): the
 * counts and one row per incident. Structural to-dos (unbound, needs calibration) are not
 * incidents: they are the Wall's To finish list (console DDD G2), never rows here.
 *
 * While Central's scheduler loop is stale or stopped, the liveness alarms (health `cause`
 * "liveness") collapse into ONE causal row: Players may be unable to report while it is
 * not ticking, so listing each silent frame would blame the equipment. The row says "may":
 * a stopped scheduler that holds no lock still accepts reports until the last offers expire
 * (slice 1 §8). The planning rows (`planningRows`) come first, whether or not any
 * Frame is silent: planning that fails or a Scene it refused is Central's, never a Frame's.
 *
 * Frame rows come first, then Player rows: the host incidents of Bound Players, from the
 * shell's fleet host read when it did not fail (none from a failed or skipped read).
 * `players` counts the Players with an incident; `severity` is the worst incident's tier
 * (a Frame alarm or a Player alarm is "alarm", Unknown-only Player incidents "unknown"); `hostsUnread` is true while the shell's
 * fleet host read is mounted but has failed or not yet loaded (none is mounted before
 * sign-in is confirmed).
 *
 * @param {object} snapshot
 * @param {{scheduler: string|null, steps?: object}|null} central
 * @param {import("./fleetHosts.js").FleetHosts|null} [hosts] the shell's fleet host read
 * @param {{devices?: Map<string, object>}|null} [bootFacts] for the Players' names
 * @returns {{frameCount: number, awaiting: number, alarms: Array<object>, players: number,
 *            hostsUnread: boolean, severity: "alarm"|"unknown"|"ok", rows: AttentionRow[],
 *            central: number}}
 */
export function attentionView(snapshot, central, hosts = null, bootFacts = null) {
  const { frameCount, awaiting, alarms } = wallAttention(snapshot);
  const stalled = central?.scheduler ?? null;
  const silenced =
    stalled === null ? [] : alarms.filter((entry) => entry.health.cause === "liveness");
  const hostsUnread = hosts !== null && (hosts.failed || hosts.read === null);
  const incidents = hostIncidents(snapshot, hostsUnread ? null : hosts?.read ?? null, bootFacts);
  const planning = planningRows(snapshot, central);
  const rows = [
    ...planning,
    ...(silenced.length > 0
      ? [
          {
            key: "scheduler",
            text:
              `${frames(silenced.length)} silent — Central's scheduler is ` +
              `${stalled.replaceAll("_", " ")}; Players may be unable to report until it recovers.`,
          },
        ]
      : []),
    ...alarms.filter((entry) => !silenced.includes(entry)).map(
      ({ frame, health }) => ({
        key: frame.id,
        frameId: frame.id,
        health,
        severity: health.severity,
        text: `${frame.id} — ${health.label}`,
      }),
    ),
    ...incidents,
  ];
  const players = new Set(incidents.map((incident) => incident.deviceId)).size;
  // The strip's tier is its worst incident (§62): an Unknown-only Player ("Never reported")
  // never turns it red.
  const severity = alarms.length > 0 || planning.length > 0 ? "alarm" : incidentSeverity(incidents);
  return { frameCount, awaiting, alarms, players, hostsUnread, severity, rows,
    central: planning.length };
}
