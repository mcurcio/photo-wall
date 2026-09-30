import { useCallback, useEffect, useRef, useState } from "react";

import { apiWrite } from "./apiWrite.js";

const REREAD_MS = 30000;

/** Fleet facts are a separate read from the inventory/Runtime snapshot. */
export function useFleetFacts(snapshot) {
  const [view, setView] = useState({ data: null, unavailable: false, loading: true, readAt: null });
  const lastRef = useRef(null);
  const inFlightRef = useRef(false);

  const refresh = useCallback(async (force = false) => {
    if (inFlightRef.current) return;
    const now = Date.now();
    if (!force && lastRef.current !== null && now - lastRef.current < REREAD_MS) return;
    lastRef.current = now;
    inFlightRef.current = true;
    try {
      const result = await apiWrite("/v1/operator/fleet", { method: "GET" });
      if (!result.ok || !result.data || !Array.isArray(result.data.devices)) {
        throw new Error(result.error ?? String(result.status));
      }
      setView({ data: result.data, unavailable: false, loading: false, readAt: now });
    } catch {
      // Keep the last successful view, but mark its age and failure separately.
      setView((current) => ({ ...current, unavailable: true, loading: false }));
    } finally {
      inFlightRef.current = false;
    }
  }, []);

  useEffect(() => {
    if (snapshot !== null) void refresh();
  }, [snapshot?.at, refresh]);

  return { ...view, refresh: () => refresh(true) };
}

export function appArtifact(release) {
  if (release?.deployable === false || !release?.app?.sha256 || !Number.isSafeInteger(release.app.size) || release.app.size < 1) {
    return null;
  }
  return { tag: release.tag, sha256: release.app.sha256, size: release.app.size };
}

export function fleetPolicyRequest(artifact, expectedRevision) {
  return apiWrite("/v1/operator/fleet/app-policy", {
    method: "PUT",
    body: { target: artifact, expected_revision: expectedRevision },
  });
}

export function deviceOverrideRequest(deviceId, artifact, expectedRevision) {
  return apiWrite(`/v1/operator/fleet/devices/${encodeURIComponent(deviceId)}/app-override`, {
    method: "PUT",
    body: { target: artifact, expected_revision: expectedRevision },
  });
}

export function clearDeviceOverrideRequest(deviceId, expectedRevision) {
  return apiWrite(`/v1/operator/fleet/devices/${encodeURIComponent(deviceId)}/app-override`, {
    method: "DELETE",
    body: { expected_revision: expectedRevision },
  });
}

export function refreshReleaseCatalog() {
  return apiWrite("/v1/operator/app/releases/refresh", { method: "POST" });
}

export function baseBaselineRequest(tag, expectedRevision) {
  return apiWrite("/v1/operator/fleet/base-baseline", {
    method: "PUT",
    body: { tag, expected_revision: expectedRevision },
  });
}
