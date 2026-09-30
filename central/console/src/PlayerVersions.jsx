import React, { useEffect, useState } from "react";

import {
  appArtifact,
  baseBaselineRequest,
  clearDeviceOverrideRequest,
  deviceOverrideRequest,
  fleetPolicyRequest,
  refreshReleaseCatalog,
  useFleetFacts,
} from "./fleetApi.js";
import { useMutate } from "./useMutate.js";

function version(artifact) {
  return artifact ? `${artifact.tag} · ${artifact.sha256.slice(0, 12)}` : "Unknown";
}

function observed(fact, fallback = "No report") {
  if (!fact) return fallback;
  const state = String(fact.state ?? fact.tag ?? "Reported").replaceAll("_", " ");
  const digest = fact.digest ? ` · ${fact.digest.slice(0, 12)}` : "";
  const provenance = fact.source === "claim" ? " · serial claim"
    : fact.source === "legacy" ? " · legacy record"
      : fact.source && fact.source !== "none" ? ` · ${fact.source}` : "";
  const assurance = fact.assurance && fact.assurance !== "none"
    ? ` · ${String(fact.assurance).replaceAll("_", " ")}` : "";
  const age = Number.isFinite(fact.age_seconds)
    ? ` · ${Math.max(0, Math.round(fact.age_seconds))} s ago`
    : fact.at != null ? ` · reported at ${fact.at}` : "";
  return `${state}${digest}${provenance}${assurance}${age}`;
}

function readTime(value) {
  const time = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(time.getTime()) ? "unknown" : time.toLocaleString();
}

function writeMessage(result, success) {
  if (result?.ok) return success;
  if (result?.status === 409) return "Policy changed while this page was open. Refresh and choose again.";
  if (result?.status >= 400 && result?.status < 500) {
    return `Central refused this selection: ${result.error ?? result.status}.`;
  }
  return "The request outcome is unknown. Refresh before trying again.";
}

/** Equipment's app release policy and independently timed fleet observations. */
export function PlayerVersions({ snapshot }) {
  const facts = useFleetFacts(snapshot);
  const mutate = useMutate();
  const [fleetPick, setFleetPick] = useState("");
  const [basePick, setBasePick] = useState("");
  const [devicePicks, setDevicePicks] = useState(() => new Map());
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);
  const view = facts.data;
  const releases = (view?.releases ?? []).filter((release) => appArtifact(release) !== null);
  const devices = view?.devices ?? [];
  const bases = (view?.releases ?? []).filter((release) => release.base?.sha256 && release.base_cache_state === "cached");

  useEffect(() => {
    if (releases.length === 0) return;
    const selected = view?.fleet_policy?.target?.tag;
    setFleetPick((current) => {
      if (releases.some((release) => release.tag === current)) return current;
      return releases.some((release) => release.tag === selected) ? selected : releases[0].tag;
    });
  }, [view]);

  useEffect(() => {
    if (bases.length === 0) return;
    const selected = view?.base_baseline?.tag;
    setBasePick((current) => {
      if (bases.some((release) => release.tag === current)) return current;
      return bases.some((release) => release.tag === selected) ? selected : bases[0].tag;
    });
  }, [view]);

  const refresh = async () => {
    setNotice(null);
    await facts.refresh();
  };

  const submit = async (request, success) => {
    if (busy) return;
    setBusy(true);
    let result;
    try {
      result = await mutate(request);
    } catch {
      result = null;
    }
    setNotice(writeMessage(result, success));
    await facts.refresh();
    setBusy(false);
  };

  const chooseFleet = () => {
    const artifact = appArtifact(releases.find((release) => release.tag === fleetPick));
    if (artifact) {
      submit(
        () => fleetPolicyRequest(artifact, view?.fleet_policy?.revision ?? 0),
        `Fleet target selected: ${artifact.tag}. Eligible devices adopt it on a later boot once bytes are available.`,
      );
    }
  };

  const chooseBase = () => {
    if (!bases.some((release) => release.tag === basePick)) return;
    submit(
      () => baseBaselineRequest(basePick, view?.base_baseline?.revision ?? 0),
      `Boot baseline selected: ${basePick}. Future PXE offers use this operator selection.`,
    );
  };

  const chooseDevice = (device) => {
    const tag = devicePicks.get(device.device_id) ?? device.desired?.artifact?.tag ?? fleetPick;
    const artifact = appArtifact(releases.find((release) => release.tag === tag));
    if (artifact) {
      submit(
        () => deviceOverrideRequest(device.device_id, artifact, device.override_revision ?? 0),
        `App target selected for ${device.device_id}. Central will show whether this base can enforce it.`,
      );
    }
  };

  const refreshCatalog = () => submit(
    refreshReleaseCatalog,
    "Release discovery requested. Refresh this view after mirroring completes.",
  );

  return (
    <section className="fleet" aria-label="Player versions">
      <div className="fleet__heading">
        <h2>Player versions</h2>
        <button type="button" disabled={busy} onClick={refresh}>Refresh status</button>
        <button type="button" disabled={busy} onClick={refreshCatalog}>Check for releases</button>
      </div>
      {facts.loading && <p>Loading Player versions…</p>}
      {facts.unavailable && <p role="alert">Fleet status is unavailable; any values below may be stale.</p>}
      {view !== null && (
        <>
          <p className="fleet__note">
            Desired policy, offered bytes, installed package, running process and display reports are separate facts.
            {view.read_at != null && ` Fleet read at ${readTime(view.read_at)}.`}
          </p>
          <div className="fleet__policy">
            <p>
              Fleet target: {version(view.fleet_policy?.target)}
              {view.fleet_policy?.source === "legacy_promotion" && " · legacy promotion policy"}
            </p>
            {releases.length === 0 ? (
              <p>No selectable app artifacts are in the release catalog.</p>
            ) : (
              <span className="fleet__controls">
                <label>Choose fleet app
                  <select value={fleetPick} onChange={(event) => setFleetPick(event.target.value)}>
                    {releases.map((release) => (
                      <option key={release.tag} value={release.tag}>
                        {release.tag}{release.available_now ? " · catalog available" : ` · ${release.mirror_state ?? "mirror unknown"}`}
                      </option>
                    ))}
                  </select>
                </label>
                <button type="button" disabled={busy || !fleetPick} onClick={chooseFleet}>Set fleet target</button>
              </span>
            )}
            <button
              type="button"
              disabled={busy || (view?.fleet_policy?.source === "explicit" && view?.fleet_policy?.target === null)}
              onClick={() => submit(
                () => fleetPolicyRequest(null, view?.fleet_policy?.revision ?? 0),
                "Fleet initial app disabled for future offers. Existing Players keep their current app.",
              )}
            >Disable initial app</button>
          </div>
          <div className="fleet__policy">
            <p>Boot baseline: {view.base_baseline?.tag ?? "No operator selection"} · operator selected, not automatically qualified</p>
            {bases.length === 0 ? (
              <p>No cached base image is selectable.</p>
            ) : (
              <span className="fleet__controls">
                <label>Choose boot baseline
                  <select value={basePick} onChange={(event) => setBasePick(event.target.value)}>
                    {bases.map((release) => <option key={release.tag} value={release.tag}>{release.tag}</option>)}
                  </select>
                </label>
                <button type="button" disabled={busy || !basePick} onClick={chooseBase}>Set boot baseline</button>
              </span>
            )}
          </div>
          <ul className="fleet__devices">
            {devices.map((device) => {
              const pick = devicePicks.get(device.device_id) ?? device.desired?.artifact?.tag ?? fleetPick;
              return (
                <li key={device.device_id} className="fleet__device">
                  <h3>{device.device_id}</h3>
                  <p>Base: {observed(device.base, "OS telemetry unavailable")}</p>
                  <p>App: {observed(device.app, "App control unconfirmed")}</p>
                  <p>Desired: {version(device.desired?.artifact)} · {device.desired?.source ?? "none"}</p>
                  <p>Latest offer: {device.offered
                    ? `${device.offered.app_digest?.slice(0, 12) ?? "no app selected"} · ${String(device.offered.app_status ?? "offer only").replaceAll("_", " ")} · offer only`
                    : "No offer recorded"}</p>
                  <p>Installed: {observed(device.installed, "Unknown")}</p>
                  <p>Running: {observed(device.running, "Unknown")}{device.running?.linkage === "unknown" && " · boot linkage unknown"}</p>
                  <p>Output: {observed(device.output, "No current presentation report")}</p>
                  <p>Fallback: {String(device.fallback ?? "unknown").replaceAll("_", " ")}
                    {device.accepted_fallback?.sha256 && ` · recorded ${device.accepted_fallback.sha256.slice(0, 12)}`}
                  </p>
                  {device.capability?.startsWith("offer_v") ? (
                    <p className="fleet__note">Offer-aware boot requested. Base reports are serial claims; this view has no direct proof of the installed app or visible output.</p>
                  ) : (
                    <p className="fleet__note">App-only target is queued; this legacy or unknown base cannot enforce it.</p>
                  )}
                  <span className="fleet__controls">
                    <label>App for {device.device_id}
                      <select
                        value={pick || ""}
                        disabled={busy || releases.length === 0}
                        onChange={(event) => setDevicePicks((current) => new Map(current).set(device.device_id, event.target.value))}
                      >
                        {releases.map((release) => <option key={release.tag} value={release.tag}>{release.tag}</option>)}
                      </select>
                    </label>
                    <button type="button" disabled={busy || !pick} onClick={() => chooseDevice(device)}>Set for Player</button>
                    {device.override !== null && (
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => submit(
                          () => clearDeviceOverrideRequest(device.device_id, device.override_revision ?? 0),
                          `App override cleared for ${device.device_id}.`,
                        )}
                      >Clear override</button>
                    )}
                  </span>
                  {!device.update_now?.available && (
                    <p className="fleet__note">Update now unavailable: {device.update_now?.reason ?? "management trust not commissioned"}.</p>
                  )}
                </li>
              );
            })}
          </ul>
        </>
      )}
      {notice && <p role="status" className="fleet__notice">{notice}</p>}
    </section>
  );
}
