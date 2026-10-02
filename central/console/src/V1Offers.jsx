import React, { useEffect, useState } from "react";

import { FactLine } from "./FactLine.jsx";
import { fact, words } from "./facts.js";
import {
  appArtifact,
  baseBaselineRequest,
  cancelMaintenanceRequest,
  clearDeviceOverrideRequest,
  deviceOverrideRequest,
  fleetPolicyRequest,
  refreshReleaseCatalog,
  useFleetFacts,
} from "./fleetApi.js";
import { ManagementFacts } from "./ManagementFacts.jsx";
import { useMutate } from "./useMutate.js";

/**
 * The V1 lane (console DDD §9, Q2: kept, labelled "V1 boot offers"): what future V1 boot
 * offers carry, read from `/v1/operator/fleet` every 30 s (fleetApi.js `useFleetFacts`).
 *
 *   V1FleetPolicy     the fleet V1 app target and V1 boot baseline, at the top of the
 *                     Players list
 *   V1PlayerSection   one Player's V1 app target, latest V1 offer, T0 claims, fallback, the
 *                     V1 records (ManagementFacts) and a queued maintenance request
 *
 * Every V1 value is Central's record (`set`) or a T0 serial claim (`claimed`): none of it
 * proves what a Player runs or shows. No control here creates a maintenance request,
 * because nothing executes one; a request already queued stays visible with Cancel.
 */

const short = (digest) => (digest ? digest.slice(0, 12) : "unknown");

function version(artifact) {
  return artifact ? `${artifact.tag} · ${short(artifact.sha256)}` : "none";
}

function readTime(value) {
  const time = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(time.getTime()) ? "unknown" : time.toLocaleString();
}

function writeMessage(result, success) {
  if (result?.ok) return success;
  if (result?.status === 409) return "The V1 record changed while this page was open. Press Refresh and choose again.";
  if (result?.status >= 400 && result?.status < 500) {
    return `Central refused this selection: ${result.error ?? result.status}.`;
  }
  return "The request outcome is unknown. Press Refresh before trying again.";
}

/** One writer for the V1 lane: one write at a time, its outcome, then a fleet re-read. */
function useV1Write(fleet) {
  const mutate = useMutate();
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(/** @type {string|null} */ (null));
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
    await fleet.refresh();
    setBusy(false);
  };
  return { busy, notice, submit };
}

/** Releases whose app artifact can be a V1 app target. */
function appReleases(view) {
  return (view?.releases ?? []).filter((release) => appArtifact(release) !== null);
}

/** "V1 fleet read as of <time>", and whether the last read failed. */
function FleetReadTime({ fleet }) {
  const at = fleet.data?.read_at;
  return (
    <p className="player__read-time">
      {at == null ? "V1 fleet read: not read yet" : `V1 fleet read as of ${readTime(at)}`}
      {fleet.unavailable && (fleet.data === null ? ", read failed" : ", refresh failed")}
    </p>
  );
}

/** A box's row in the fleet read, or null. */
export function v1Device(fleet, deviceId) {
  return (fleet.data?.devices ?? []).find((candidate) => candidate.device_id === deviceId) ?? null;
}

/**
 * The latest V1 boot offer Central issued to a box: a record, never proof it booted.
 * Unknown while the fleet read has not answered; null when no V1 offer is recorded.
 */
export function v1OfferFact(fleet, deviceId) {
  if (fleet.data === null) {
    return fact({ kind: "unknown", why: fleet.unavailable ? "the fleet read failed" : "not read yet" });
  }
  const offered = v1Device(fleet, deviceId)?.offered;
  if (!offered) return null;
  return fact({
    kind: "set",
    value: `Issued for boot ${offered.boot_id} · app ${offered.app_digest ? short(offered.app_digest) : "none"} (${words(offered.app_status)}), not proof the Player booted`,
    receivedAt: offered.at, readAt: fleet.data.read_at,
  });
}

/** A T0 OS app observation: a serial claim Central accepted but cannot verify. */
function t0Claim(observation, offeredBootId, readAt, what) {
  if (!observation) return fact({ kind: "unknown", why: `no T0 ${what} claim recorded` });
  const parts = [`app ${short(observation.digest)} ${what}`];
  if (observation.reason) parts.push(`reason: ${words(observation.reason)}`);
  if (observation.boot_id) {
    parts.push(`boot ${observation.boot_id}`);
    if (offeredBootId && offeredBootId !== observation.boot_id) parts.push("not the latest V1 offer's boot");
  } else {
    parts.push("boot unknown");
  }
  if (observation.boot_ambiguity) parts.push("multiple boot claims");
  const process = observation.process;
  if (process && Number.isSafeInteger(process.pid)) parts.push(`PID ${process.pid}`);
  const age = Number.isFinite(observation.age_seconds) ? Math.max(0, observation.age_seconds) : null;
  return fact({
    kind: "claimed",
    value: parts.join(" · "),
    source: observation.source === "serial_claim" ? "its serial check-in" : words(observation.source),
    // The latest check-in's receipt (every check-in records one), not when the claim first came.
    receipt: "latest",
    receivedAt: age === null || typeof readAt !== "number" ? null : readAt - age,
    readAt,
  });
}

/**
 * The fleet V1 policy (Players list, top): the V1 app target and V1 boot baseline future
 * V1 boot offers carry. Its own fleet read.
 *
 * @param {{snapshot: object}} props
 */
export function V1FleetPolicy({ snapshot }) {
  const fleet = useFleetFacts(snapshot);
  const { busy, notice, submit } = useV1Write(fleet);
  const [appPick, setAppPick] = useState("");
  const [basePick, setBasePick] = useState("");
  const view = fleet.data;
  const releases = appReleases(view);
  const bases = (view?.releases ?? []).filter((release) => release.base?.sha256 && release.base_cache_state === "cached");

  // Keep a still-offered pick; otherwise start from Central's current selection.
  useEffect(() => {
    const keep = (current, options, selected) => {
      if (options.some((release) => release.tag === current)) return current;
      if (options.some((release) => release.tag === selected)) return selected;
      return options[0]?.tag ?? "";
    };
    setAppPick((current) => keep(current, releases, view?.fleet_policy?.target?.tag));
    setBasePick((current) => keep(current, bases, view?.base_baseline?.tag));
  }, [view]);

  const setFleetTarget = () => {
    const artifact = appArtifact(releases.find((release) => release.tag === appPick));
    if (artifact) {
      submit(
        () => fleetPolicyRequest(artifact, view?.fleet_policy?.revision ?? 0),
        `V1 fleet app target set: ${artifact.tag}. Future V1 boot offers carry it once its bytes are available; no running Player changes.`,
      );
    }
  };

  const setBaseline = () => {
    if (!bases.some((release) => release.tag === basePick)) return;
    submit(
      () => baseBaselineRequest(basePick, view?.base_baseline?.revision ?? 0),
      `V1 boot baseline set: ${basePick}. Future V1 boot offers use it.`,
    );
  };

  const disabled = view?.fleet_policy?.source === "explicit" && view?.fleet_policy?.target === null;
  return (
    <section className="fleet" role="region" aria-label="V1 boot offers">
      <div className="fleet__heading">
        <h2>V1 boot offers</h2>
        <button type="button" disabled={busy} onClick={() => fleet.refresh()}>Refresh V1 records</button>
        <button
          type="button"
          disabled={busy}
          onClick={() => submit(refreshReleaseCatalog, "Release discovery requested. Press Refresh after mirroring completes.")}
        >Check for releases</button>
      </div>
      <p className="fleet__note">
        What future V1 boot offers carry. A V1 record is not proof of what any Player runs or shows.
      </p>
      <FleetReadTime fleet={fleet} />
      {fleet.loading && <p>Loading V1 records…</p>}
      {view !== null && (
        <>
          <div className="fleet__policy">
            <FactLine
              label="V1 fleet app target"
              fact={fact({
                kind: "set",
                value: view.fleet_policy?.target
                  ? `${version(view.fleet_policy.target)}${view.fleet_policy.source === "legacy_promotion" ? " · legacy promotion" : ""}`
                  : "none",
              })}
            />
            {releases.length === 0 ? (
              <p>No selectable app artifacts are in the release catalog.</p>
            ) : (
              <span className="fleet__controls">
                <label>V1 fleet app
                  <select value={appPick} onChange={(event) => setAppPick(event.target.value)}>
                    {releases.map((release) => (
                      <option key={release.tag} value={release.tag}>
                        {release.tag}{release.available_now ? " · catalog available" : ` · ${release.mirror_state ?? "mirror unknown"}`}
                      </option>
                    ))}
                  </select>
                </label>
                <button type="button" disabled={busy || !appPick} onClick={setFleetTarget}>Set V1 fleet target</button>
              </span>
            )}
            <button
              type="button"
              disabled={busy || disabled}
              onClick={() => submit(
                () => fleetPolicyRequest(null, view.fleet_policy?.revision ?? 0),
                "V1 initial app disabled for future V1 boot offers. Players keep their current app.",
              )}
            >Disable V1 initial app</button>
          </div>
          <div className="fleet__policy">
            <FactLine
              label="V1 boot baseline"
              fact={fact({ kind: "set", value: view.base_baseline?.tag
                ? `${view.base_baseline.tag} · operator selected, not automatically qualified` : "none" })}
            />
            {bases.length === 0 ? (
              <p>No cached base image is selectable.</p>
            ) : (
              <span className="fleet__controls">
                <label>V1 boot baseline
                  <select value={basePick} onChange={(event) => setBasePick(event.target.value)}>
                    {bases.map((release) => <option key={release.tag} value={release.tag}>{release.tag}</option>)}
                  </select>
                </label>
                <button type="button" disabled={busy || !basePick} onClick={setBaseline}>Set V1 boot baseline</button>
              </span>
            )}
          </div>
        </>
      )}
      {notice && <p role="status" className="fleet__notice">{notice}</p>}
    </section>
  );
}

/**
 * One Player's V1 boot offers (Player page): its V1 app target (set or clear its
 * override), its latest V1 offer, its T0 installed and running claims, its fallback, its V1
 * records and any queued maintenance request, which can only be canceled.
 *
 * @param {{deviceId: string, fleet: ReturnType<typeof useFleetFacts>}} props
 */
export function V1PlayerSection({ deviceId, fleet }) {
  const { busy, notice, submit } = useV1Write(fleet);
  const [pick, setPick] = useState(/** @type {string|null} */ (null));
  const view = fleet.data;
  const device = v1Device(fleet, deviceId);
  const releases = appReleases(view);
  if (view === null) {
    return (
      <>
        <FleetReadTime fleet={fleet} />
        {fleet.loading && <p>Loading V1 records…</p>}
      </>
    );
  }
  if (device === null) {
    return (
      <>
        <FleetReadTime fleet={fleet} />
        <p className="roster__empty">Central holds no V1 fleet record for this box.</p>
      </>
    );
  }
  const desired = device.desired;
  const chosen = pick ?? desired?.artifact?.tag ?? releases[0]?.tag ?? "";
  const offer = v1OfferFact(fleet, deviceId);
  const request = device.maintenance_request;

  const setTarget = () => {
    const artifact = appArtifact(releases.find((release) => release.tag === chosen));
    if (artifact) {
      submit(
        () => deviceOverrideRequest(deviceId, artifact, device.override_revision ?? 0),
        `V1 app target set for this Player: ${artifact.tag}. Its future V1 boot offers carry it.`,
      );
    }
  };

  return (
    <>
      <FleetReadTime fleet={fleet} />
      <FactLine
        label="V1 app target"
        fact={fact({ kind: "set", value: `${version(desired?.artifact)} · ${words(desired?.source ?? "none")}` })}
      />
      <span className="fleet__controls">
        <label>V1 app for this Player
          <select value={chosen} disabled={busy || releases.length === 0} onChange={(event) => setPick(event.target.value)}>
            {releases.map((release) => <option key={release.tag} value={release.tag}>{release.tag}</option>)}
          </select>
        </label>
        <button type="button" disabled={busy || !chosen} onClick={setTarget}>Set V1 app target</button>
        {device.override != null && (
          <button
            type="button"
            disabled={busy}
            onClick={() => submit(
              () => clearDeviceOverrideRequest(deviceId, device.override_revision ?? 0),
              "V1 app target for this Player cleared; it follows the V1 fleet app target.",
            )}
          >Clear V1 app target</button>
        )}
      </span>
      <FactLine label="Latest V1 boot offer" fact={offer ?? fact({ kind: "set", value: "No V1 boot offer recorded" })} />
      <FactLine label="T0 installed" fact={t0Claim(device.installed, device.offered?.boot_id, view.read_at, "installed")} />
      <FactLine label="T0 running" fact={t0Claim(device.running, device.offered?.boot_id, view.read_at, "running")} />
      <FactLine
        label="Fallback"
        fact={fact({ kind: "set", value: `${words(device.fallback)}${device.accepted_fallback?.sha256 ? ` · recorded ${short(device.accepted_fallback.sha256)}` : ""}` })}
      />
      <ManagementFacts management={device.management} readAt={view.read_at} />
      {request && (
        <div className="fleet__policy">
          <FactLine
            label="V1 maintenance request"
            fact={fact({ kind: "set", value: `${words(request.status)} · ${version(request.target)} · expires ${readTime(request.expires_at)}${request.reason ? ` · ${words(request.reason)}` : ""}` })}
          />
          <p className="fleet__note">Nothing executes maintenance requests on this Central; a queued request only records intent.</p>
          {request.status === "queued" && (
            <button
              type="button"
              disabled={busy}
              onClick={() => submit(
                () => cancelMaintenanceRequest(deviceId, request.request_id, request.revision),
                "V1 maintenance request canceled.",
              )}
            >Cancel maintenance request</button>
          )}
        </div>
      )}
      {notice && <p role="status" className="fleet__notice">{notice}</p>}
    </>
  );
}
