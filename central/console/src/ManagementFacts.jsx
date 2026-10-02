import React from "react";

function words(value) {
  return String(value ?? "unknown").replaceAll("_", " ");
}

function digest(value) {
  return value ? value.slice(0, 12) : "unknown";
}

function when(value) {
  if (!Number.isFinite(value)) return "time unknown";
  return new Date(value * 1000).toLocaleString();
}

/** Central attempt bookkeeping and authenticated OS claims, never pixel health. */
export function ManagementFacts({ management }) {
  if (!management) return <p className="fleet__note">Loader OS management details are unavailable from this Central.</p>;
  const { attempt, os_session: session, latest_attempt_report: report } = management;
  return (
    <div className="fleet__policy" aria-label="Loader OS management facts">
      <p>Loader OS session: {session?.state === "none" ? "No commissioned session recorded"
        : `${words(session?.state)} · ${String(session?.trust_mode ?? "unknown").toUpperCase()} · expires ${when(session?.expires_at)} · physical connectivity unknown`}</p>
      <p>Central app attempt: {attempt?.state === "none" ? "No current generation attempt record"
        : `${words(attempt?.state)} · target ${digest(attempt?.target_digest)} · fallback ${digest(attempt?.fallback_digest)} · byte retention required, availability unknown`}
        {attempt?.selection === "queued_record_by_created_at" &&
          ` · one of ${attempt.queued_count} queued records, selected by creation time`}
        {attempt?.selection === "nonqueued_by_created_at" && attempt.nonqueued_count > 1 &&
          ` · one of ${attempt.nonqueued_count} retained nonqueued records, selected by creation time`}
        {attempt?.attempt_revoked_at != null &&
          ` · attempt revoked ${when(attempt.attempt_revoked_at)}; repair remains recorded`}
      </p>
      <p>Authenticated OS attempt claim: {report?.state === "reported"
        ? `${words(report.executor_state)} · active ${digest(report.active_digest)} · ${words(report.carrier_session_relation)} · received ${when(report.received_at)} · output unknown`
        : report?.state === "none" ? "No report recorded"
          : `${words(report?.state)} · no execution conclusion`}
        {report?.fault_code && ` · fault ${words(report.fault_code)}`}
      </p>
    </div>
  );
}
