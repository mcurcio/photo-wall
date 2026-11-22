/**
 * What Home lists after Central's own problems, in words (decision 0036, "What Home replaces"):
 * a Pi's own fault from the fleet host read, and the photo app's report of what it could not
 * prepare. Central's status does not carry these yet, so the console's other reads find them; this
 * module is the one place Home words them, by the same rules as Central's problems: the box is
 * "Pi 3f2a9c" and a Frame is its name, never "Player" or the host service's own name outside
 * Details (design language §7).
 *
 * Pure, with no markup: the page only renders what it says.
 */
import type { Verdict } from "../patterns/health-badge.tsx";

/** One host incident as hostHealth.js `hostIncidents` lists it. */
export interface HostIncident {
  key: string;
  deviceId: string;
  /** The Frames the Pi feeds, by id. */
  frames: readonly string[];
  severity: "alarm" | "unknown";
  /** The box's state in the host read, and its judged item in the fleet pages' own words. */
  state: "not_read" | "never" | "silent" | "reporting";
  words: string;
  /** The fleet pages' whole line for it: internal words, for Details only. */
  text: string;
  hardwareHref: string;
}

/** One card of Home's list that is not Central's own problem. */
export interface ReportView {
  key: string;
  subject: string;
  verdict: Verdict;
  what: string;
  /** Internal words, shown only under Details; null: none. */
  details: string | null;
}

const feeds = (names: readonly string[]) => (names.length === 0 ? "" : ` It feeds ${names.join(", ")}.`);

/**
 * A Pi's own fault as Home says it. `pi` is "Pi 3f2a9c" (null: "A Pi"); `names` its Frames' names.
 * A Pi that reports says what it reports; one that does not is said plainly, with the fleet page's
 * own line under Details.
 */
export function piFaultView(incident: HostIncident, pi: string | null, names: readonly string[]): ReportView {
  const subject = pi ?? "A Pi";
  const silent = incident.state !== "reporting";
  return {
    key: incident.key,
    subject,
    verdict: { severity: incident.severity, label: incident.severity === "alarm" ? "Pi fault" : "No Pi report", receipt: null },
    what: (incident.state === "never" ? "It has not reported its own health yet."
      : silent ? "It has stopped reporting its own health."
        : `It reports: ${incident.words}.`) + feeds(names),
    details: incident.text,
  };
}

/** The photo app's report of what it could not prepare for a Frame, by the Frame's name. */
export function readinessView(report: { frameId: string; messages: readonly string[]; age: string | null }, name: string | null): ReportView {
  return {
    key: `readiness:${report.frameId}`,
    subject: name ?? report.frameId,
    verdict: { severity: "notice", label: "Reported by the photo app", receipt: report.age === null ? null : `${report.age} ago` },
    what: report.messages.join(" "),
    details: null,
  };
}
