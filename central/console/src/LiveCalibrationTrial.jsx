import React, { useCallback, useEffect, useRef, useState } from "react";
import { apiWrite } from "./apiWrite.js";
import { useMutate } from "./useMutate.js";

const refusal = {
  trial_display_unavailable: "Central holds no current Display Host session for this Player. Check that the Player is running.",
  trial_current_output_required: "Central has no Display Host report for this Output in the last 10 seconds, or no current app process link for this Frame. Retry when the Output is connected and the Player app is running.",
  trial_admitted_surface_required: "Display Host has not admitted and acknowledged the current app surface yet. Wait, then retry.",
  trial_latest_not_presented: "Display Host has not acknowledged the latest edit. Save calibration stays unavailable.",
  trial_sequence_conflict: "Another request changed this live calibration. Refresh its status before editing again.",
  trial_already_active: "Live calibration is already running for this Frame. Wait for it to stop or expire.",
  trial_frame_unbound: "Bind this Frame to an Output before starting live calibration.",
};
const geometryKey = (value) => JSON.stringify([value.corners, value.crop, value.rotation, value.gain]);

export function useCalibrationCapability(frameId, generation) {
  const [capability, setCapability] = useState(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let current = true;
    setCapability(null);
    apiWrite(`/v1/operator/frames/${encodeURIComponent(frameId)}/calibration-capability`, { method: "GET" })
      .then((result) => { if (current) setCapability(result.ok ? result.data : { mode: "unavailable" }); })
      .catch(() => { if (current) setCapability({ mode: "unavailable" }); });
    return () => { current = false; };
  }, [frameId, generation, retry]);
  return [capability, () => setRetry((value) => value + 1)];
}

/**
 * Live calibration with Display Host acknowledgment (`native_trial`; console DDD §20). The
 * existing editor owns the draft; this component owns only the leased session Central calls a
 * calibration trial. Save calibration is enabled only once Display Host acknowledges that the
 * latest edit was presented to the compositor (R7), which is not proof of what the Panel shows.
 */
export function LiveCalibrationTrial({ frameId, trying, calibrated }) {
  const [row, setRow] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const gate = useRef(false);
  const current = useRef(null);
  const alive = useRef(true);
  const mutate = useMutate();
  current.current = row;
  const base = `/v1/operator/frames/${encodeURIComponent(frameId)}/calibration-trials`;
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
      if (alive.current) setError("Central did not answer. Live calibration still ends when its lease expires.");
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
  return <section className="facet__section facet__section--lease" role="group" aria-label="Live calibration">
    <h4 className="facet__subtitle">Live calibration</h4>
    <p className="facet__note">Central sends each edit to Display Host while live calibration runs. Save calibration is available only after Display Host acknowledges that the latest edit was presented to the compositor. The provisional limits are 5 seconds without an edit and 30 seconds total; once it expires, start again.</p>
    {!calibrated && <p className="facet__note">First calibration uses a synthetic canvas. Its neutral starting transform is provisional until you Save; it does not enable Scene playback.</p>}
    <div className="calib__actions">
      {!active && <button type="button" className="calib__action" disabled={busy} onClick={() => run("begin")}>{row ? "Start again" : "Start live calibration"}</button>}
      {active && <>
        <button type="button" className="calib__action" disabled={busy || !presented} onClick={() => run("save")}>Save calibration</button>
        <button type="button" className="calib__action" disabled={busy} onClick={() => run("end")}>Stop live calibration</button>
      </>}
    </div>
    <p role="status">{active ? (changed ? "Draft edit pending." : presented ? `Edit ${row.sequence} presented to the compositor by Display Host · not proof of what the Panel shows` : `Waiting for Display Host to acknowledge edit ${row.sequence}.`) : row ? `Live calibration ${row.state}; your draft is kept` : "Start when Display Host reports the bound Output connected and the app surface admitted."}</p>
    {error && <p role="alert">{error} <button type="button" disabled={busy} onClick={() => { setError(null); if (active) run("status"); }}>Retry status</button></p>}
  </section>;
}
