import React, { useCallback, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { useMutate } from "./useMutate.js";

/**
 * The Sources region (moved out of Showrunner.jsx, pass 2 slice 3 §3): the
 * Source-configuration form and the list of saved live queries, each with a
 * Refresh that re-runs its query server-side. A Source is a saved live QUERY
 * named `name:rev` (design D-e) — NOT a downloaded album, and never something a
 * Player browses or links to.
 *
 * @param {{snapshot: object|null}} props
 */
export function SourcesRegion({ snapshot }) {
  const sources = snapshot?.media?.sources ?? [];
  const mutate = useMutate();

  // Refresh re-runs a saved query: POST …/sources/{ref}/refresh via the shared
  // apiWrite helper, wrapped in useMutate() (primitive #7) so Plane A — and
  // therefore this Sources list — refreshes exactly once after the write. The
  // ref is `name:rev` and may contain a colon, so it is path-encoded.
  const refreshSource = useCallback(
    (sourceRef) =>
      mutate(() =>
        apiWrite(
          `/v1/operator/sources/${encodeURIComponent(sourceRef)}/refresh`,
          { method: "POST" },
        ),
      ),
    [mutate],
  );

  return (
    <section className="showrunner__region" role="region" aria-label="Sources">
      <h2 className="showrunner__region-title">Sources</h2>
      {/* A Source is a saved live query (name:rev), re-run on Refresh — never
          a downloaded album, and the Player never sees it (design D-e). */}
      <p className="showrunner__region-note">
        Each Source is a saved live query. Refresh re-runs the query.
      </p>
      {/* Bead G2 (SR-source-config): CONFIGURE a new Source — the create the
          legacy page had and Bead 13's list+Refresh lacked (content-parity
          GAP 2). A saved live query, never a downloaded album (design D-e). */}
      <SourceConfiguration />
      {sources.length === 0 ? (
        <p className="showrunner__empty">No Sources yet.</p>
      ) : (
        <ul className="showrunner__sources" role="list">
          {sources.map((source) => (
            <li key={source.source_ref} className="showrunner__source">
              <dl className="record">
                <dt>Source</dt>
                <dd className="showrunner__source-ref">{source.source_ref}</dd>
                <dt>Status</dt>
                <dd className="showrunner__source-status">{source.status}</dd>
                {/* A never-refreshed Source has no last_success yet: it is
                    "Awaiting refresh" until its saved query is first re-run
                    (same honest readout the legacy page showed). */}
                <dt>Refreshed</dt>
                <dd className="showrunner__source-refreshed">
                  {source.last_success
                    ? `Last refreshed ${new Date(source.last_success * 1000).toLocaleString()}`
                    : "Awaiting refresh"}
                </dd>
              </dl>
              <div className="record__actions">
                <button
                  type="button"
                  className="showrunner__refresh"
                  aria-label={`Refresh ${source.source_ref}`}
                  onClick={() => refreshSource(source.source_ref)}
                >
                  Refresh
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/**
 * The stored `SourceSpec` write body (media/models.py:32-40) for a saved live
 * query: `schema` (alias of schema_version, populate_by_name), the `source_ref`
 * (`name:rev`), its `connection_ref`, and the `media_types` subset. `favorites`
 * and the capture window are optional (default null / unbounded) and omitted
 * here — a Source is defined by its query identity + connection + kinds, and the
 * server applies its own defaults for the rest.
 *
 * A single "both" choice maps to the full ["image", "video"] subset; otherwise
 * the one chosen kind. There is deliberately NO album/Immich/open-in field: a
 * Source is a saved live query, not a downloaded album (design D-e).
 *
 * @param {{sourceRef: string, connectionRef: string, mediaType: string}} draft
 * @returns {{schema: number, source_ref: string, connection_ref: string, media_types: string[]}}
 */
export function buildSourceSpec({ sourceRef, connectionRef, mediaType }) {
  return {
    schema: 1,
    source_ref: sourceRef,
    connection_ref: connectionRef,
    media_types: mediaType === "both" ? ["image", "video"] : [mediaType],
  };
}

/**
 * The Source-configuration form (Bead G2 — SR-source-config), closing
 * content-parity GAP 2: the legacy flat page could CREATE a Source, but the
 * console (Bead 13) could only list + Refresh. This restores create BEFORE the
 * cutover.
 *
 * A Source is a saved live QUERY named `name:rev` (design D-e) — never a
 * downloaded album, an Immich link, or anything a Player browses/opens; this
 * form carries no such language. It saves the query via
 * `PUT /v1/operator/sources/{ref}` (the `source_ref` path segment is `name:rev`
 * and may contain a colon, so it is path-encoded) with the stored `SourceSpec`
 * body. The write wraps the shared `useMutate()` hook (primitive #7) so Plane A
 * — and therefore the Sources list above — refreshes exactly once; the new
 * Source then appears by its `name:rev` identity, awaiting its first Refresh.
 *
 * The server answers with a `SourceConfigurationReceipt` ({source_ref, created});
 * `created` is true for a first configuration and false if the exact same spec
 * already existed (an idempotent re-save), which we report honestly.
 */
function SourceConfiguration() {
  const mutate = useMutate();

  // Plane B: component-local source-configuration draft.
  const [sourceRef, setSourceRef] = useState("");
  const [connectionRef, setConnectionRef] = useState("");
  const [mediaType, setMediaType] = useState("both");
  const [status, setStatus] = useState(/** @type {string|null} */ (null));
  const [saving, setSaving] = useState(false);

  const valid =
    !saving && sourceRef.trim() !== "" && connectionRef.trim() !== "";

  const saveSource = useCallback(async () => {
    const ref = sourceRef.trim();
    setSaving(true);
    setStatus(null);
    try {
      const result = await mutate(() =>
        apiWrite(`/v1/operator/sources/${encodeURIComponent(ref)}`, {
          method: "PUT",
          body: buildSourceSpec({
            sourceRef: ref,
            connectionRef: connectionRef.trim(),
            mediaType,
          }),
        }),
      );
      setStatus(
        result.ok
          ? `Saved Source ${ref}.`
          : `Could not save Source: ${result.error ?? result.status}.`,
      );
    } catch {
      setStatus("Could not save Source: the request did not complete.");
    } finally {
      setSaving(false);
    }
  }, [sourceRef, connectionRef, mediaType, mutate]);

  return (
      <form
        className="source-config"
        role="form"
        aria-label="Configure a Source"
        onSubmit={(event) => {
          event.preventDefault();
          if (valid) {
            saveSource();
          }
        }}
      >
        <label className="source-config__field">
          Source name and revision
          <input
            type="text"
            className="source-config__ref"
            aria-label="Source name and revision"
            value={sourceRef}
            onChange={(event) => setSourceRef(event.target.value)}
          />
        </label>

        <label className="source-config__field">
          Connection name
          <input
            type="text"
            className="source-config__connection"
            aria-label="Connection name"
            value={connectionRef}
            onChange={(event) => setConnectionRef(event.target.value)}
          />
        </label>

        <label className="source-config__field">
          Media type
          <select
            className="source-config__type"
            aria-label="Media type"
            value={mediaType}
            onChange={(event) => setMediaType(event.target.value)}
          >
            <option value="both">Images and video</option>
            <option value="image">Images only</option>
            <option value="video">Video only</option>
          </select>
        </label>

        <button type="submit" className="source-config__save" disabled={!valid}>
          Save source
        </button>

        {status !== null ? (
          <p className="source-config__status" role="status">
            {status}
          </p>
        ) : null}
      </form>
  );
}
