import React from "react";

import { FactLine } from "./FactLine.jsx";
import { fact, words } from "./facts.js";

function digest(value) {
  return value ? value.slice(0, 12) : "unknown";
}

function when(value) {
  if (!Number.isFinite(value)) return "time unknown";
  return new Date(value * 1000).toLocaleString();
}

/** The V1 loader OS session: Central's record (`set`). */
function loaderSession(session) {
  if (session?.state === "none") return fact({ kind: "set", value: "No loader OS session recorded" });
  return fact({ kind: "set", value: `${words(session?.state)} · ${String(session?.trust_mode ?? "unknown").toUpperCase()}`
    + ` · expires ${when(session?.expires_at)} · physical connectivity unknown` });
}

/** The V1 app attempt: Central's attempt bookkeeping (`set`). */
function appAttempt(attempt) {
  if (attempt?.state === "none") return fact({ kind: "set", value: "No current generation attempt record" });
  const parts = [words(attempt?.state), `target ${digest(attempt?.target_digest)}`,
    `fallback ${digest(attempt?.fallback_digest)}`, "byte retention required, availability unknown"];
  if (attempt?.selection === "queued_record_by_created_at") {
    parts.push(`one of ${attempt.queued_count} queued records, selected by creation time`);
  }
  if (attempt?.selection === "nonqueued_by_created_at" && attempt.nonqueued_count > 1) {
    parts.push(`one of ${attempt.nonqueued_count} retained nonqueued records, selected by creation time`);
  }
  if (attempt?.attempt_revoked_at != null) {
    parts.push(`attempt revoked ${when(attempt.attempt_revoked_at)}; repair remains recorded`);
  }
  return fact({ kind: "set", value: parts.join(" · ") });
}

/**
 * The authenticated OS attempt claim: what the box's serial check-in claimed (`claimed`, its
 * latest receipt on Central's clock). With no usable report, Central's record says so (`set`).
 */
function attemptClaim(report, readAt) {
  const fault = report?.fault_code ? ` · fault ${words(report.fault_code)}` : "";
  if (report?.state === "reported") {
    return fact({ kind: "claimed", source: "its serial check-in", receipt: "latest",
      value: `${words(report.executor_state)} · active ${digest(report.active_digest)}`
        + ` · ${words(report.carrier_session_relation)} · output unknown${fault}`,
      receivedAt: report.received_at, readAt });
  }
  if (report?.state === "none") return fact({ kind: "set", value: "No report recorded" });
  return fact({ kind: "set", value: `${words(report?.state)} · no execution conclusion${fault}` });
}

/**
 * The V1 loader's records (console DDD §9 V1 boot offers, §15): Central's attempt bookkeeping
 * and an authenticated OS claim, never pixel health, each through `fact()` (rule 2). Every
 * label names V1 so none is mistaken for a V2 node session.
 *
 * @param {{management: object|null|undefined, readAt: number|null}} props `readAt` is the
 *   fleet read's `read_at` (Central's clock), for the claim's receipt age
 */
export function ManagementFacts({ management, readAt }) {
  if (!management) {
    return <FactLine label="V1 loader OS management"
      fact={fact({ kind: "unknown", why: "this Central does not serve the V1 loader's records" })} />;
  }
  const { attempt, os_session: session, latest_attempt_report: report } = management;
  return (
    <div className="fleet__policy" role="group" aria-label="V1 records">
      <FactLine label="V1 loader OS session" fact={loaderSession(session)} />
      <FactLine label="V1 app attempt" fact={appAttempt(attempt)} />
      <FactLine label="V1 authenticated OS attempt claim" fact={attemptClaim(report, readAt)} />
    </div>
  );
}
