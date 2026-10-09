import React from "react";

import { wallAttention } from "./health.js";
import { hostIncidents, incidentSeverity } from "./hostHealth.js";

/** "1 frame", "3 frames": the scheduler row counts silent frames this way. */
export function frames(count) {
  return `${count} ${count === 1 ? "frame" : "frames"}`;
}

const counted = (count, noun) => `${count} ${noun}${count === 1 ? "" : "s"}`;

/**
 * The strip's one summary (console DDD §61): incidents only, Frames then Players ("2 Frames ·
 * 1 Player need attention", "1 Player needs attention"), else "No Frame or Player needs
 * attention", with the awaiting count. While the fleet host read has failed or not yet loaded
 * it ends " · host health not read", so missing host rows never read as health.
 *
 * @param {{alarms: Array<object>, players: number, awaiting: number, hostsUnread: boolean}} view
 * @returns {string}
 */
export function attentionSummary({ alarms, players, awaiting, hostsUnread }) {
  const parts = [
    ...(alarms.length > 0 ? [counted(alarms.length, "Frame")] : []),
    ...(players > 0 ? [counted(players, "Player")] : []),
  ];
  const single = parts.length === 1 && alarms.length + players === 1;
  const text = parts.length === 0
    ? `No Frame or Player needs attention${awaiting > 0 ? ` · ${awaiting} awaiting a first report` : ""}`
    : `${parts.join(" · ")} ${single ? "needs" : "need"} attention`;
  return hostsUnread ? `${text} · host health not read` : text;
}

/**
 * @typedef {{key: string, text: string, severity?: string, frameId?: string,
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
 * When Central's scheduler is neither "ok" nor "disabled", the liveness alarms (health
 * `cause` "liveness") collapse into ONE causal row: Players may be unable to report
 * while it is not ok, so listing each silent frame would blame the equipment. The row
 * says "may": a stopped scheduler that holds no lock still accepts reports until the
 * last offers expire (slice 1 §8). It is the only row without a frame.
 *
 * Frame rows come first, then Player rows: the host incidents of Bound Players, from the
 * shell's fleet host read when it did not fail (none from a failed or skipped read).
 * `players` counts the Players with an incident; `severity` is the worst incident's tier
 * (a Frame alarm or a Player alarm is "alarm", Unknown-only Player incidents "unknown"); `hostsUnread` is true while the shell's
 * fleet host read is mounted but has failed or not yet loaded (node control off mounts none,
 * and its banner names that).
 *
 * @param {object} snapshot
 * @param {{scheduler: string|null}|null} central
 * @param {import("./fleetHosts.js").FleetHosts|null} [hosts] the shell's fleet host read
 * @param {{devices?: Map<string, object>}|null} [bootFacts] for the Players' names
 * @returns {{frameCount: number, awaiting: number, alarms: Array<object>, players: number,
 *            hostsUnread: boolean, severity: "alarm"|"unknown"|"ok", rows: AttentionRow[]}}
 */
export function attentionView(snapshot, central, hosts = null, bootFacts = null) {
  const { frameCount, awaiting, alarms } = wallAttention(snapshot);
  const stalled = central?.scheduler ?? null;
  const silenced =
    stalled === null ? [] : alarms.filter((entry) => entry.health.cause === "liveness");
  const hostsUnread = hosts !== null && (hosts.failed || hosts.read === null);
  const incidents = hostIncidents(snapshot, hostsUnread ? null : hosts?.read ?? null, bootFacts);
  const rows = [
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
  const severity = alarms.length > 0 ? "alarm" : incidentSeverity(incidents);
  return { frameCount, awaiting, alarms, players, hostsUnread, severity, rows };
}

/** A Player row's text, then [Frame <id>]… [<Player>] (§61: links follow R4). */
function PlayerEntry({ row, entry }) {
  return (
    <>
      <span className="attention__text">{row.text}</span>
      {row.frames.map((frameId) => (
        <React.Fragment key={frameId}>
          {" "}
          {entry({ key: frameId, frameId, text: `Frame ${frameId}` })}
        </React.Fragment>
      ))}{" "}
      <a className="attention__player" href={row.hardwareHref}>{row.name}</a>
    </>
  );
}

/**
 * The list of Frames and Players needing attention: the attention strip's disclosure (capped) and
 * the full-width Needs attention page (#/attention) both render it.
 *
 * A row about a frame renders through `entry(row)` when given (a button on the Wall
 * side, a link on the Needs attention page); otherwise, and for the scheduler row, it
 * is plain text, so a Show page sees health as status only (R4). A Player row, with
 * `entry`, is its text, then `entry` for each of its Frames ("Frame <id>") and a link to
 * its Hardware page; without `entry` it too is plain text.
 *
 * @param {{rows: AttentionRow[], cap?: number, id?: string, className?: string,
 *          entry?: ((row: AttentionRow) => React.ReactNode)|null}} props
 */
export function AttentionList({ rows, cap = Infinity, id, className, entry = null }) {
  const shown = rows.slice(0, cap);
  const more = rows.length - shown.length;
  return (
    <ul id={id} className={className} aria-label="Frames and Players needing attention">
      {shown.map((row) => (
        <li key={row.key} className={`attention__item health--${row.severity ?? "alarm"}`}>
          {entry === null ? row.text
            : row.deviceId !== undefined ? <PlayerEntry row={row} entry={entry} />
              : row.frameId !== undefined ? entry(row) : row.text}
        </li>
      ))}
      {more > 0 && <li className="attention__more">{`and ${more} more`}</li>}
    </ul>
  );
}
