import React, { useCallback, useMemo, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { SceneAuthoring } from "./SceneAuthoring.jsx";
import { useMutate } from "./useMutate.js";

/**
 * The Showrunner shell (Bead 12) — the "run the show" layer of the one console
 * (design §2, J4).
 *
 * It lays out the four show-programming REGIONS — Sources, Scenes, Programs,
 * Runs — as empty shells this bead; Beads 13-16 fill them. The only hardware
 * fact the show layer is allowed to see is per-Frame `calibration_valid`,
 * rendered here as a Frame-health BADGE (a STATUS, never a control): an invalid
 * Frame cannot present, so the showrunner needs the validity flag, but every
 * Display CONTROL stays behind the Wall-mode Commissioning facet (R4, J4).
 *
 * R4 is enforced STRUCTURALLY by composition, not by a runtime `if (mode)`
 * guard: this component simply never imports or renders the Commissioning facet
 * (nor the Inspector that hosts it). There is therefore no code path — and no
 * DOM — by which Commissioning controls can appear in the show layer.
 *
 * @param {{snapshot: object|null}} props
 */
export function Showrunner({ snapshot }) {
  const frames = snapshot?.inventory?.frames ?? [];
  // A Source is a saved live QUERY named `name:rev` (design D-e) — NOT a
  // downloaded album, and never something a Player browses or links to. The
  // media plane carries the operator's saved queries; each renders as its
  // `name:rev` identity with a Refresh that re-runs the query server-side.
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
    <div className="showrunner" role="region" aria-label="Showrunner">
      {/* Frame-health badges: calibration_valid is STATUS, not a control (R4).
          One badge per Frame; an invalid Frame cannot present the show. */}
      <section
        className="showrunner__health"
        role="group"
        aria-label="Frame health"
      >
        {frames.map((frame) => {
          const valid = frame.calibration_valid === true;
          return (
            <span
              key={frame.id}
              className={`showrunner__badge showrunner__badge--${valid ? "valid" : "invalid"}`}
              aria-label={`Frame ${frame.id} calibration ${valid ? "valid" : "invalid"}`}
            >
              {`${frame.id}: ${valid ? "Calibration valid" : "Calibration invalid"}`}
            </span>
          );
        })}
      </section>

      <section className="showrunner__region" role="region" aria-label="Sources">
        <h2 className="showrunner__region-title">Sources</h2>
        {/* A Source is a saved live query (name:rev), re-run on Refresh — never
            a downloaded album, and the Player never sees it (design D-e). */}
        <p className="showrunner__region-note">
          Each Source is a saved live query. Refresh re-runs the query.
        </p>
        {sources.length === 0 ? (
          <p className="showrunner__empty">No Sources yet.</p>
        ) : (
          <ul className="showrunner__sources" role="list">
            {sources.map((source) => (
              <li key={source.source_ref} className="showrunner__source">
                <span className="showrunner__source-ref">
                  {source.source_ref}
                </span>
                <span className="showrunner__source-status">
                  {source.status}
                </span>
                <button
                  type="button"
                  className="showrunner__refresh"
                  aria-label={`Refresh ${source.source_ref}`}
                  onClick={() => refreshSource(source.source_ref)}
                >
                  Refresh
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section className="showrunner__region" role="region" aria-label="Scenes">
        <h2 className="showrunner__region-title">Scenes</h2>
        {/* Bead 14a: author + save a live-source Scene; the Scenes list appears
            here by scene_id. Bead 14b adds the authored per-frame mode. */}
        <SceneAuthoring snapshot={snapshot} />
      </section>
      <section className="showrunner__region" role="region" aria-label="Programs">
        <h2 className="showrunner__region-title">Programs</h2>
        {/* Bead 15: schedule a Program — bind a Scene to a SINGLE time window
            with a priority (PUT …/programs/{id}); list existing Programs with a
            Remove (DELETE …/programs/{id}). The optional helper creates N
            SEPARATE windows — N individual, independently-stored Programs — and
            is deliberately never described as a recurring rule: central stores
            no recurrence model (design R2, Q2), so the UI implies none. */}
        <ProgramScheduling snapshot={snapshot} />
      </section>
      <section className="showrunner__region" role="region" aria-label="Runs">
        <h2 className="showrunner__region-title">Runs</h2>
      </section>
    </div>
  );
}

/**
 * Convert a `datetime-local` field value (interpreted in the operator's local
 * timezone) to a POSIX epoch in SECONDS — the unit `Program.starts_at` /
 * `ends_at` carry (central/runtime.py:117-118, filled from `clock.utc()` =
 * time.time()). Returns NaN for an empty/unparseable field so callers can gate
 * the save on a valid window.
 *
 * @param {string} local
 * @returns {number}
 */
export function toEpochSeconds(local) {
  if (!local) {
    return NaN;
  }
  return new Date(local).getTime() / 1000;
}

/**
 * The body of a single-window Program write: exactly the stored `Program` shape
 * (central/runtime.py:114-125) — a Scene bound to ONE `[starts_at, ends_at)`
 * window with a priority. There is deliberately no recurrence field: central
 * stores single windows only (design Q2), so N-window scheduling is N of THESE,
 * never one recurring rule.
 *
 * @param {{programId: string, sceneId: string, startsAt: number, endsAt: number, priority: number}} draft
 * @returns {{program_id: string, scene_id: string, starts_at: number, ends_at: number, priority: number}}
 */
export function buildProgram({ programId, sceneId, startsAt, endsAt, priority }) {
  return {
    program_id: programId,
    scene_id: sceneId,
    starts_at: startsAt,
    ends_at: endsAt,
    priority,
  };
}

/**
 * The Programs region body (Bead 15 — SR-programs).
 *
 * A Program binds a Scene to a SINGLE time window with a priority (design J4).
 * The form saves one Program via `PUT /v1/operator/programs/{id}` (a stored
 * single-window Program), the list shows every stored Program with a Remove
 * (`DELETE /v1/operator/programs/{id}`), and an OPTIONAL helper creates N
 * SEPARATE windows in one action — N independent, individually-stored Programs
 * (each a real single-window `PUT`), which the operator then manages and removes
 * one by one.
 *
 * HONESTY (design R2 / Q2): central stores no recurrence model, so NOTHING here
 * is a "recurring rule". The N-window helper is described only as creating
 * separate windows / individual Programs — never a recurrence — because the
 * stored reality is exactly N discrete Programs, nothing that keeps recurring on
 * its own.
 *
 * All writes wrap in the shared `useMutate()` hook (primitive #7) so Plane A —
 * and therefore the Programs list below — refreshes exactly once after a write.
 *
 * @param {{snapshot: object|null}} props
 */
function ProgramScheduling({ snapshot }) {
  // Scenes to bind come from the runtime definitions map (same source the Scenes
  // region reads); a Program can only reference a Scene that exists.
  const definitions = snapshot?.runtime?.definitions ?? {};
  const scenes = useMemo(() => Object.values(definitions), [definitions]);
  // Stored Programs are the runtime programs map, keyed by program_id
  // (central/app.py:670 -> runtime.export_state()["programs"]).
  const programsMap = snapshot?.runtime?.programs ?? {};
  const programs = useMemo(() => Object.values(programsMap), [programsMap]);

  const mutate = useMutate();

  // Plane B: component-local scheduling draft.
  const [programId, setProgramId] = useState("");
  const [sceneId, setSceneId] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [priority, setPriority] = useState(0);
  // The optional N-window helper: how many SEPARATE windows to create at once.
  const [windowCount, setWindowCount] = useState(3);
  const [status, setStatus] = useState(/** @type {string|null} */ (null));
  const [saving, setSaving] = useState(false);

  const startsAt = toEpochSeconds(start);
  const endsAt = toEpochSeconds(end);
  const windowValid =
    Number.isFinite(startsAt) && Number.isFinite(endsAt) && endsAt > startsAt;
  const baseValid =
    !saving && programId.trim() !== "" && sceneId !== "" && windowValid;

  const count = Math.max(1, Math.floor(Number(windowCount) || 0));

  const saveProgram = useCallback(async () => {
    const id = programId.trim();
    setSaving(true);
    setStatus(null);
    try {
      const result = await mutate(() =>
        apiWrite(`/v1/operator/programs/${encodeURIComponent(id)}`, {
          method: "PUT",
          body: buildProgram({
            programId: id,
            sceneId,
            startsAt,
            endsAt,
            priority: Number(priority),
          }),
        }),
      );
      setStatus(
        result.ok
          ? `Scheduled Program ${id}.`
          : `Could not schedule Program: ${result.error ?? result.status}.`,
      );
    } catch {
      setStatus("Could not schedule Program: the request did not complete.");
    } finally {
      setSaving(false);
    }
  }, [programId, sceneId, startsAt, endsAt, priority, mutate]);

  // The N-window helper: create `count` SEPARATE Programs in one action, each a
  // real stored single-window Program (id `<base>-<n>`), the whole window shifted
  // by n × the base window's duration so the windows do not overlap. This is N
  // discrete Programs, NOT a recurring rule — each must be managed and removed on
  // its own below.
  const addSeparateWindows = useCallback(async () => {
    const id = programId.trim();
    const duration = endsAt - startsAt;
    setSaving(true);
    setStatus(null);
    try {
      const result = await mutate(() =>
        Promise.all(
          Array.from({ length: count }, (_unused, index) => {
            const windowId = `${id}-${index + 1}`;
            const offset = index * duration;
            return apiWrite(
              `/v1/operator/programs/${encodeURIComponent(windowId)}`,
              {
                method: "PUT",
                body: buildProgram({
                  programId: windowId,
                  sceneId,
                  startsAt: startsAt + offset,
                  endsAt: endsAt + offset,
                  priority: Number(priority),
                }),
              },
            );
          }),
        ),
      );
      const failed = result.filter((r) => !r.ok).length;
      setStatus(
        failed === 0
          ? `Created ${count} separate Programs.`
          : `Created ${count - failed} of ${count} separate Programs.`,
      );
    } catch {
      setStatus("Could not create the windows: a request did not complete.");
    } finally {
      setSaving(false);
    }
  }, [programId, sceneId, startsAt, endsAt, priority, count, mutate]);

  const removeProgram = useCallback(
    async (id) => {
      await mutate(() =>
        apiWrite(`/v1/operator/programs/${encodeURIComponent(id)}`, {
          method: "DELETE",
        }),
      );
    },
    [mutate],
  );

  return (
    <div className="program-scheduling">
      <form
        className="program-scheduling__form"
        role="form"
        aria-label="Schedule a Program"
        onSubmit={(event) => {
          event.preventDefault();
          if (baseValid) {
            saveProgram();
          }
        }}
      >
        <label className="program-scheduling__field">
          Program ID
          <input
            type="text"
            className="program-scheduling__program-id"
            aria-label="Program ID"
            value={programId}
            onChange={(event) => setProgramId(event.target.value)}
          />
        </label>

        <label className="program-scheduling__field">
          Scene
          <select
            className="program-scheduling__scene"
            aria-label="Scene"
            value={sceneId}
            onChange={(event) => setSceneId(event.target.value)}
          >
            <option value="">Choose a Scene</option>
            {scenes.map((scene) => (
              <option key={scene.scene_id} value={scene.scene_id}>
                {scene.scene_id}
              </option>
            ))}
          </select>
        </label>

        <label className="program-scheduling__field">
          Window start
          <input
            type="datetime-local"
            className="program-scheduling__start"
            aria-label="Window start"
            value={start}
            onChange={(event) => setStart(event.target.value)}
          />
        </label>

        <label className="program-scheduling__field">
          Window end
          <input
            type="datetime-local"
            className="program-scheduling__end"
            aria-label="Window end"
            value={end}
            onChange={(event) => setEnd(event.target.value)}
          />
        </label>

        <label className="program-scheduling__field">
          Priority
          <input
            type="number"
            className="program-scheduling__priority"
            aria-label="Priority"
            value={priority}
            onChange={(event) => setPriority(event.target.value)}
          />
        </label>

        <button
          type="submit"
          className="program-scheduling__save"
          disabled={!baseValid}
        >
          Schedule Program
        </button>
      </form>

      {/* Optional helper (design Q2): create N SEPARATE windows at once. Each is
          a real, independently-stored single-window Program — this is N discrete
          Programs to manage individually, and it is intentionally NOT a
          recurring rule (central stores no recurrence model). */}
      <fieldset
        className="program-scheduling__multi"
        aria-label="Create separate windows"
      >
        <legend>Create separate windows</legend>
        <p className="program-scheduling__multi-note">
          Creates {count} separate windows — {count} individual Programs, each
          stored on its own and removed one by one below.
        </p>
        <label className="program-scheduling__field">
          Number of windows
          <input
            type="number"
            min="1"
            className="program-scheduling__count"
            aria-label="Number of windows"
            value={windowCount}
            onChange={(event) => setWindowCount(event.target.value)}
          />
        </label>
        <button
          type="button"
          className="program-scheduling__multi-add"
          aria-label="Add separate windows"
          disabled={!baseValid}
          onClick={addSeparateWindows}
        >
          {`Add ${count} separate windows`}
        </button>
      </fieldset>

      {status !== null ? (
        <p className="program-scheduling__status" role="status">
          {status}
        </p>
      ) : null}

      {/* The Programs list: every stored Program by its program_id, with its
          single window, scene, priority, and a Remove. Each row is one discrete
          Program — the operator manages and removes them individually. */}
      {programs.length === 0 ? (
        <p className="program-scheduling__empty">No Programs yet.</p>
      ) : (
        <ul className="program-scheduling__programs" role="list">
          {programs.map((program) => (
            <li
              key={program.program_id}
              className="program-scheduling__program"
              aria-label={`Program ${program.program_id}`}
            >
              <span className="program-scheduling__program-id-text">
                {program.program_id}
              </span>
              <span className="program-scheduling__program-scene">
                {`Scene ${program.scene_id}`}
              </span>
              <span className="program-scheduling__program-window">
                {`${new Date(program.starts_at * 1000).toLocaleString()} – ${new Date(program.ends_at * 1000).toLocaleString()}`}
              </span>
              <span className="program-scheduling__program-priority">
                {`Priority ${program.priority}`}
              </span>
              <button
                type="button"
                className="program-scheduling__remove"
                aria-label={`Remove program ${program.program_id}`}
                onClick={() => removeProgram(program.program_id)}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
