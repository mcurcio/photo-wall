import React, { useCallback, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { localDayStart, sourceProblems } from "./authoring.js";
import { Field, ProblemSummary, useProblems } from "./Field.jsx";
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

const FAVOURITES = { any: null, only: true, not: false };

/**
 * The stored `SourceSpec` write body (media/models.py `SourceSpec`) for a saved
 * live query: `schema` (alias of schema_version), the `source_ref` (`name:rev`),
 * its `connection_ref`, the `media_types` subset, and the filters the API
 * already accepts (§7): `favorites` (Any / Only / Not → null / true / false)
 * and the capture window (`captured_from` / `captured_until`, the local
 * midnights of the chosen days; "until" is exclusive). An unset filter is
 * omitted, so the server applies its default.
 *
 * A single "both" choice maps to the full ["image", "video"] subset; otherwise
 * the one chosen kind. There is deliberately NO album/Immich/open-in field: a
 * Source is a saved live query, not a downloaded album (design D-e), and
 * `SourceSpec` has no album filter.
 *
 * @param {{sourceRef: string, connectionRef: string, mediaType: string,
 *          favorites?: "any"|"only"|"not", capturedFrom?: string,
 *          capturedUntil?: string}} draft
 * @returns {object}
 */
export function buildSourceSpec({
  sourceRef,
  connectionRef,
  mediaType,
  favorites = "any",
  capturedFrom = "",
  capturedUntil = "",
}) {
  const spec = {
    schema: 1,
    source_ref: sourceRef,
    connection_ref: connectionRef,
    media_types: mediaType === "both" ? ["image", "video"] : [mediaType],
  };
  if (FAVOURITES[favorites] !== null) {
    spec.favorites = FAVOURITES[favorites];
  }
  const from = localDayStart(capturedFrom);
  const until = localDayStart(capturedUntil);
  if (from !== null) {
    spec.captured_from = from;
  }
  if (until !== null) {
    spec.captured_until = until;
  }
  return spec;
}

/**
 * The Source-configuration form (Bead G2 — SR-source-config; slice 3 §6, §7).
 *
 * A Source is a saved live QUERY named `name:rev` (design D-e) — never a
 * downloaded album, an Immich link, or anything a Player browses/opens; this
 * form carries no such language. It saves the query via
 * `PUT /v1/operator/sources/{ref}` (the `source_ref` path segment is `name:rev`
 * and may contain a colon, so it is path-encoded) with the stored `SourceSpec`
 * body, inside `useMutate()` (primitive #7). Problems are reasons beside the
 * fields; only the in-flight save disables the button.
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
  const [favorites, setFavorites] = useState(/** @type {"any"|"only"|"not"} */ ("any"));
  const [capturedFrom, setCapturedFrom] = useState("");
  const [capturedUntil, setCapturedUntil] = useState("");
  const [status, setStatus] = useState(/** @type {string|null} */ (null));
  const [saving, setSaving] = useState(false);
  const problems = useProblems(
    sourceProblems({ sourceRef, connectionRef, capturedFrom, capturedUntil }),
  );

  const field = (setter, key) => (event) => {
    setter(event.target.value);
    if (key !== null) {
      problems.touch(key);
    }
  };

  const saveSource = async () => {
    if (saving || !problems.check()) {
      return;
    }
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
            favorites,
            capturedFrom,
            capturedUntil,
          }),
        }),
      );
      if (result.ok) {
        setSourceRef("");
        setConnectionRef("");
        setMediaType("both");
        setFavorites("any");
        setCapturedFrom("");
        setCapturedUntil("");
        problems.reset();
        setStatus(`Saved Source ${ref}.`);
      } else {
        setStatus(`Could not save Source: ${result.error ?? result.status}.`);
      }
    } catch {
      setStatus("Could not save Source: the request did not complete.");
    } finally {
      setSaving(false);
    }
  };

  const text = (key, label, value, setter, hint = null) => (
    <Field id={problems.idFor(key)} label={label} hint={hint} reason={problems.reasonFor(key)}>
      {(props) => (
        <input
          {...props}
          type="text"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          value={value}
          onChange={field(setter, key)}
        />
      )}
    </Field>
  );

  const day = (key, label, value, setter, hint) => (
    <Field id={problems.idFor(key)} label={label} hint={hint} reason={problems.reasonFor(key)}>
      {(props) => <input {...props} type="date" value={value} onChange={field(setter, key)} />}
    </Field>
  );

  return (
    <form
      className="source-config"
      role="form"
      aria-label="Configure a Source"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        saveSource();
      }}
    >
      <ProblemSummary summary={problems.summary} label="Source problems" />
      {text("ref", "Source name and revision", sourceRef, setSourceRef, "Like holiday:1.")}
      {text("connection", "Connection name", connectionRef, setConnectionRef)}

      <Field id={problems.idFor("type")} label="Media type">
        {(props) => (
          <select {...props} value={mediaType} onChange={field(setMediaType, null)}>
            <option value="both">Images and video</option>
            <option value="image">Images only</option>
            <option value="video">Video only</option>
          </select>
        )}
      </Field>

      <Field id={problems.idFor("favorites")} label="Favourites">
        {(props) => (
          <select {...props} value={favorites} onChange={field(setFavorites, null)}>
            <option value="any">Any</option>
            <option value="only">Only favourites</option>
            <option value="not">Not favourites</option>
          </select>
        )}
      </Field>

      {day("from", "Taken from", capturedFrom, setCapturedFrom, "From the start of this day.")}
      {day("until", "Taken until", capturedUntil, setCapturedUntil, "Up to the start of this day.")}

      <button type="submit" className="source-config__save" disabled={saving}>
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
