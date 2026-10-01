import React, { useCallback, useEffect, useRef, useState } from "react";
import { apiWrite } from "./apiWrite.js";
import { useMutate } from "./useMutate.js";

const refusal = {
  trial_display_unavailable: "The base display service is unavailable. Check that the Player is connected.",
  trial_current_output_required: "Waiting for current display evidence. Retry when the Output is connected.",
  trial_admitted_surface_required: "The current app surface is not admitted yet. Wait for the diagnostic handoff.",
  trial_latest_not_presented: "The latest edit has not been presented. Save remains unavailable.",
  trial_sequence_conflict: "Another request changed this Trial. Refresh its status before editing again.",
  trial_already_active: "A Trial is already active for this Frame. Wait for it to end or expire.",
  trial_frame_unbound: "Bind this Frame to an Output before beginning a Trial.",
};
const geometryKey = (value) => JSON.stringify([value.corners, value.crop, value.rotation, value.gain]);

export function useCalibrationCapability(frameId, generation) {
  const [capability, setCapability] = useState(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let current = true;
    setCapability(null);
    apiWrite(`/v2/operator/frames/${encodeURIComponent(frameId)}/calibration-capability`, { method: "GET" })
      .then((result) => { if (current) setCapability(result.ok ? result.data : { mode: "unavailable" }); })
      .catch(() => { if (current) setCapability({ mode: "unavailable" }); });
    return () => { current = false; };
  }, [frameId, generation, retry]);
  return [capability, () => setRetry((value) => value + 1)];
}

/** The existing editor owns the draft. This component owns only a leased Trial. */
export function LiveCalibrationTrial({ frameId, trying, calibrated }) {
  const [row, setRow] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const gate = useRef(false);
  const current = useRef(null);
  const alive = useRef(true);
  const mutate = useMutate();
  current.current = row;
  const base = `/v2/operator/frames/${encodeURIComponent(frameId)}/calibration-trials`;
  const run = useCallback(async (operation, calibration) => {
    if (gate.current) return;
    gate.current = true;
    setBusy(true);
    const before = current.current;
    try {
      const request = () => apiWrite(operation === "begin" ? base : `${base}/${before.trial_id}`, {
        method: "POST",
        body: operation === "begin" ? {} : {
          operation, expected_sequence: before.sequence,
          ...(calibration ? { calibration } : {}),
        },
      });
      const result = operation === "save" ? await mutate(request) : await request();
      if (!alive.current) return;
      if (!result.ok) {
        setError(refusal[result.error] ?? result.error ?? `Request failed (${result.status}).`);
        return;
      }
      current.current = result.data;
      setRow(result.data);
      setError(null);
    } catch {
      if (alive.current) setError("Display connection unavailable. The local Trial lease still expires.");
    } finally {
      gate.current = false;
      if (alive.current) setBusy(false);
    }
  }, [base, mutate]);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);
  const active = row?.state === "active";
  const draftKey = geometryKey(trying);
  const deliveredKey = row ? geometryKey(row.calibration) : null;
  const changed = active && draftKey !== deliveredKey;
  useEffect(() => {
    if (!active || !changed || busy || error) return;
    const timer = setTimeout(() => run("edit", {
      ...row.calibration, ...trying, revision: row.calibration_revision,
    }), 180);
    return () => clearTimeout(timer);
  }, [active, changed, busy, draftKey, deliveredKey, error, row, run, trying]);
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => run("status"), 500);
    return () => clearInterval(timer);
  }, [active, run]);
  const presented = active && row.presented_sequence === row.sequence &&
    row.presented_sha256 === row.candidate_sha256 && !changed;
  return <section className="facet__section facet__section--lease" role="group" aria-label="Live calibration trial">
    <h4 className="facet__subtitle">Live calibration trial</h4>
    <p className="facet__note">Edits appear on the display during a short operational Trial. Save is available only after the latest edit has a compositor presentation receipt. The provisional limits are 5 seconds without an edit and 30 seconds total; an expired Trial must be started again.</p>
    {!calibrated && <p className="facet__note">First calibration uses a synthetic canvas. Its neutral starting transform is provisional until you Save; it does not enable Scene playback.</p>}
    <div className="calib__actions">
      {!active && <button type="button" className="calib__action" disabled={busy} onClick={() => run("begin")}>{row ? "Start another Trial" : "Begin Trial"}</button>}
      {active && <>
        <button type="button" className="calib__action" disabled={busy || !presented} onClick={() => run("save")}>Save calibration</button>
        <button type="button" className="calib__action" disabled={busy} onClick={() => run("end")}>End Trial</button>
      </>}
    </div>
    <p role="status">{active ? (changed ? "Draft edit pending." : presented ? `Edit ${row.sequence} presented on the display.` : `Waiting for presentation of edit ${row.sequence}.`) : row ? `Trial ${row.state}. Your draft is retained.` : "Begin when the bound display is connected and admitted."}</p>
    {error && <p role="alert">{error} <button type="button" disabled={busy} onClick={() => { setError(null); if (active) run("status"); }}>Retry status</button></p>}
  </section>;
}
