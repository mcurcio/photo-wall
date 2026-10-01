import React, { useCallback, useEffect, useRef, useState } from "react";
import "./nodeDevice.css";
import { apiWrite } from "./apiWrite.js";

const label = (value) => value == null ? "Unknown" : String(value).replaceAll("_", " ");
const utc = (value) => value == null ? "Unknown" : new Date(value * 1000).toLocaleString();

/** Equipment operations use the node owners' projections, never Player liveness. */
export function NodeDevicePanel({ deviceId }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [message, setMessage] = useState(null);
  const [busy, setBusy] = useState(false);
  const [audit, setAudit] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [reboot, setReboot] = useState(null);
  const [rebootResult, setRebootResult] = useState(null);
  const alive = useRef(true);
  const reading = useRef(false);
  const writing = useRef(false);
  const base = `/v1/operator/node/devices/${encodeURIComponent(deviceId)}`;
  const refresh = useCallback(async () => {
    if (reading.current) return;
    reading.current = true;
    try {
      const results = await Promise.all([
        apiWrite("/v1/operator/node/status", { method: "GET" }),
        apiWrite(base, { method: "GET" }),
        apiWrite(`${base}/app-attempts`, { method: "GET" }),
      ]);
      if (!alive.current) return;
      const failure = results.find((result) => !result.ok);
      if (failure) {
        setError(failure.error ?? `Node status unavailable (${failure.status}).`);
      } else {
        setData({ gate: results[0].data, device: results[1].data, attempts: results[2].data,
          loadedAt: performance.now() });
        setError(null);
      }
    } catch {
      if (alive.current) setError("Node status unavailable. Previously read facts may be out of date.");
    } finally { reading.current = false; }
  }, [base]);
  useEffect(() => {
    alive.current = true;
    if (!open) return () => { alive.current = false; };
    refresh();
    const timer = setInterval(refresh, 3000);
    return () => { alive.current = false; clearInterval(timer); };
  }, [open, refresh]);
  const sessions = data?.device.sessions ?? [];
  const eligible = sessions.filter((session) => session.current && session.command_eligible
    && session.scope === "operator_reboot");
  const selected = eligible.find((session) => session.session_id === sessionId);
  const claims = data?.device.boot_claims ?? [];
  const gateOpen = !error && data?.gate.transport_enabled
    && data.gate.effect_gate.effective_state === "open";
  const reviewReboot = () => {
    if (!selected || !gateOpen || !audit.trim()) return;
    const elapsed = performance.now() - data.loadedAt;
    const estimated = selected.expires_boottime_ms
      - (selected.expires_at - data.device.read_at) * 1000 + elapsed;
    const expiry = Math.min(selected.expires_boottime_ms, Math.floor(estimated + 20000));
    if (expiry <= estimated + 1000) {
      setMessage("This session is expiring. Refresh and choose a current session."); return;
    }
    setReboot({ command_id: crypto.randomUUID(), session_id: selected.session_id,
      device_generation: data.device.device_generation, operator_audit_ref: audit.trim(),
      rollout_generation: data.gate.effect_gate.generation, expires_boottime_ms: expiry,
      valid_for_seconds: 30 });
    setRebootResult(null);
    setMessage(null);
  };
  const requestReboot = async () => {
    if (writing.current) return;
    writing.current = true;
    setBusy(true);
    try {
      const result = await apiWrite(`${base}/reboots`, { method: "POST", body: reboot });
      if (!alive.current) return;
      const unknown = result.status >= 500;
      setRebootResult(unknown ? "unknown" : result.ok ? "recorded" : "refused");
      setMessage(unknown ? "Reboot request outcome unknown. Refresh the audit or retry this exact request."
        : result.ok ? "Reboot command recorded. This does not establish that a reboot occurred."
          : `Reboot request refused: ${result.error ?? result.status}. The existing request was not retargeted.`);
      await refresh();
    } catch {
      if (alive.current) {
        setRebootResult("unknown");
        setMessage("Reboot request outcome unknown. Retry preserves the exact command and session.");
      }
    } finally {
      writing.current = false;
      if (alive.current) setBusy(false);
    }
  };
  return <section className="node-device" aria-label={`Node operations for ${deviceId}`}>
    <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>Node status and controls</button>
    {open && <div>
      <p>Physical output: {label(data?.device.physical_output)}. Host samples and command responses do not establish visible pixels.</p>
      {error && <p role="alert">{error}</p>}
      <button type="button" onClick={refresh} disabled={busy}>Refresh node status</button>
      {data && <>
        <p>Read {utc(data.device.read_at)} · Device generation {data.device.device_generation} · Effects {label(data.gate.effect_gate.effective_state)}</p>
        <p>{label(data.gate.effect_gate.reason)}. Command eligibility is checked by Central when the request is issued and dispatched.</p>
        <details><summary>Observed processes and host samples</summary>
          {sessions.length === 0 && <p>No node sessions observed.</p>}
          {sessions.map((session) => <article key={session.session_id}>
            <p>{label(session.producer.owner)} · {session.current ? "Current session" : "Historical or expired session"} · {label(session.scope)}</p>
            <p>Boot <code>{session.producer.kernel_boot_id}</code> · Session <code>{session.session_id}</code></p>
            <p>Commands: {session.command_eligible && session.current ? "Eligible for its scope" : label(session.command_reason ?? "unavailable")}</p>
            {session.host_observation ? <>
              <p>Host sample received {utc(session.host_observation.received_at)} ({Math.floor(session.host_observation.receipt_age_seconds)} seconds before this read). Sample boot time {session.host_observation.sample.sampled_boottime_ms} ms.</p>
              <ul>{session.host_observation.sample.metrics.map((metric) => <li key={`${metric.source}/${metric.name}`}>{label(metric.name)}: {metric.value} {metric.unit} ({label(metric.source)})</li>)}</ul>
              {session.host_observation.sample.fault_code && <p>Reported fault: {label(session.host_observation.sample.fault_code)}</p>}
            </> : <p>Host sample: unavailable for this producer.</p>}
            {session.manager_preparation && <p>Manager preparation: {label(session.manager_preparation.sample.state)}, received {utc(session.manager_preparation.received_at)}.
              {session.manager_preparation.sample.fault && ` Fault: ${label(session.manager_preparation.sample.fault)}.`}
              {session.manager_preparation.sample.operation_id && ` Operation ${session.manager_preparation.sample.operation_id}.`}
              {" "}Preparation is not activation.</p>}
          </article>)}
        </details>
        <details><summary>App transition observations</summary>
          {(data.attempts.operations ?? []).length === 0 && <p>No app transition attempts recorded.</p>}
          {(data.attempts.operations ?? []).map((operation) => <article key={operation.operation_id}>
            <p>Operation <code>{operation.operation_id}</code>: {label(operation.state)} · Runtime {operation.runtime_fenced ? "fenced" : "not fenced"}</p>
            <p>Command response: {label(operation.command_response?.decision)}. Latest effect: {label(operation.latest_effect?.phase)}{operation.latest_effect && ` (sequence ${operation.latest_effect.sequence})`}.</p>
          </article>)}
          <p>An expired grant does not establish that an app stopped.</p>
        </details>
        <details><summary>Reboot</summary>
          <p>The latest boot to enroll is the current boot; it supersedes earlier boots of this serial.</p>
          <label>Operator audit reference <input value={audit} maxLength={256} onChange={(event) => setAudit(event.target.value)} disabled={busy} /></label>
          <label>Reboot session <select value={sessionId} disabled={busy || !!reboot} onChange={(event) => setSessionId(event.target.value)}>
            <option value="">Choose a current reboot session…</option>
            {eligible.map((session) => <option key={session.session_id} value={session.session_id}>{session.producer.kernel_boot_id} / {session.session_id}</option>)}
          </select></label>
          <button type="button" disabled={busy || !!reboot || !selected || !gateOpen || !audit.trim()} onClick={reviewReboot}>Review reboot request</button>
          {!gateOpen && <p>Reboot unavailable while the effect gate is closed or status is unavailable.</p>}
          {reboot && <div role="group" aria-label="Reboot request review">
            <p>Request reboot for session <code>{reboot.session_id}</code>, generation {reboot.device_generation}. Command <code>{reboot.command_id}</code>.</p>
            <p>The command expires at boot time {reboot.expires_boottime_ms} ms. A retry keeps this exact identity and expiry.</p>
            <button type="button" disabled={busy || !gateOpen || ["recorded", "refused"].includes(rebootResult)} onClick={requestReboot}>{rebootResult === "unknown" ? "Retry exact reboot request" : "Request reboot for this session"}</button>
            <button type="button" disabled={busy} onClick={() => { setReboot(null); setRebootResult(null); }}>Close request review</button>
          </div>}
          <h4>Reboot audit</h4>
          {(data.device.reboot_commands ?? []).length === 0 && <p>No reboot commands recorded.</p>}
          {(data.device.reboot_commands ?? []).map((command) => <article key={command.command_id}>
            <p>Command <code>{command.command_id}</code>, recorded {utc(command.issued_at)}.</p>
            <p>Responses: {(command.responses ?? []).map((response) => label(response.message.message?.decision)).join(", ") || "None observed"}.</p>
            <p>Reboot initiation evidence: {(command.effects ?? []).map((effect) => `${label(effect.state)} — ${label(effect.disposition)} (received ${utc(effect.received_at)})`).join(", ") || "None observed"}.</p>
            <p>Subsequent boot claims observed: {claims.filter((claim) => claim.first_received_at > command.issued_at && claim.kernel_boot_id !== command.command.producer.kernel_boot_id).map((claim) => claim.kernel_boot_id).join(", ") || "None"}. A later claim alone does not prove this command caused a reboot.</p>
          </article>)}
        </details>
      </>}
      {message && <p role="status">{message}</p>}
    </div>}
  </section>;
}
