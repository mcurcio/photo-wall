import React from "react";

import { FactLine } from "./FactLine.jsx";
import { receiptText, words } from "./facts.js";
import { judgeHost } from "./hostHealth.js";

// Player › Health's groups, in order (console DDD §52, §61).
const GROUPS = Object.freeze(["Thermal", "Power and throttling", "Compute", "Storage", "Network", "Software"]);
const RECEIPT = "host";
const PIXELS = "Host samples do not show visible pixels.";

/**
 * One judged host item (hostHealth.js), through the one fact renderer. Its band is a class,
 * for the eye only: the words already carry it (§5).
 *
 * @param {{entry: {label: string, fact: import("./facts.js").Fact, band?: string|null,
 *          suffix?: string}, receipt?: boolean}} props
 */
export function HostItem({ entry, receipt = true }) {
  return (
    <div className={`players__item players__item--${entry.band ?? "none"}`}>
      <FactLine label={entry.label} fact={entry.fact} suffix={entry.suffix} receipt={receipt} />
    </div>
  );
}

/**
 * The host facts record's one receipt line ("Host facts first received 3 d ago"), or why
 * there is none; the record's facts then render without their own receipt (§62).
 *
 * @param {{receipt: import("./facts.js").Fact}} props
 */
export function HostFactsReceipt({ receipt }) {
  return receipt.kind === "reported" ? (
    <p className="fact fact--reported">{`Host facts ${receiptText(receipt)}`}</p>
  ) : (
    <FactLine label="Host facts" fact={receipt} />
  );
}

/** Every metric the current boot's newest sample carries, known or not, then its fault code. */
function rawLines(host) {
  return [
    ...(host?.metrics ?? []).map((metric) =>
      `${words(metric?.name)}: ${metric?.value} ${metric?.unit} (${words(metric?.source)})`),
    ...(host?.fault_code ? [`Reported fault: ${words(host.fault_code)}`] : []),
  ];
}

/**
 * Player › Health (console DDD §52, §61, §62), the Player page's first section: the box's
 * host values from the shell's one fleet host read (G12, fleetHosts.js), judged and worded by
 * hostHealth.js `judgeHost` (`classifyHost`, `hostFactItems`), so the Players list, Needs attention and
 * this page agree. Host Management's receipt and the host facts record's one receipt line
 * come first, then the groups; an "Every reported metric" disclosure holds the raw sample
 * lines, the fault code and the pixels caveat. It reads nothing itself.
 *
 * @param {{hosts: import("./fleetHosts.js").FleetHosts, deviceId: string}} props
 */
export function HostHealthSection({ hosts, deviceId }) {
  const { row, health, facts } = judgeHost(hosts, deviceId);
  const receipt = health.items.find((item) => item.name === RECEIPT);
  const lines = rawLines(row?.host ?? null);
  return (
    <>
      {hosts.failed && (
        <p className="player__read-time">{`Last read failed: ${hosts.error?.code ?? "unanswered"}`}</p>
      )}
      {receipt !== undefined && <HostItem entry={receipt} />}
      <HostFactsReceipt receipt={facts.receipt} />
      {GROUPS.map((group) => {
        const metrics = health.items.filter((item) => item.group === group);
        const named = facts.items.filter((item) => item.group === group);
        if (metrics.length === 0 && named.length === 0) return null;
        return (
          <div key={group} className="player__host-group" role="group" aria-label={group}>
            <h4 className="player__layer-title">{group}</h4>
            {metrics.map((item) => <HostItem key={item.name} entry={item} />)}
            {named.map((item) => <HostItem key={item.name} entry={item} receipt={false} />)}
          </div>
        );
      })}
      <details className="player__details">
        <summary>Every reported metric</summary>
        <ul aria-label="Every reported metric">
          {lines.map((line, index) => <li key={`${index}-${line}`}>{line}</li>)}
          <li>{PIXELS}</li>
        </ul>
      </details>
    </>
  );
}
