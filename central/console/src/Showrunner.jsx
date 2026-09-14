import React, { useCallback, useMemo, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { rankedContributions } from "./join.js";
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
        {/* Bead 16 — SR-runs: activate a Scene now (POST …/activations) and show
            the SYNCHRONOUS {status, reason} truthfully; list the current live
            Runs (runtime.current.runs) each with Finish and Cancel
            (POST …/runs/{id}/finish|cancel); a "why" panel ranks a Frame's
            contributions by precedence. */}
        <RunControl snapshot={snapshot} />
      </section>
    </div>
  );
}

/**
 * The Runs region body (Bead 16 — SR-runs).
 *
 * Three surfaces, all honest to the server's real shapes (design J4, §5/§6):
 *
 *  1. ACTIVATE NOW — a form that POSTs `/v1/operator/activations` with the
 *     stored `ActivationRequest` body ({scene_id, activation_id, priority}). The
 *     server answers SYNCHRONOUSLY with an `Admission` ({status, reason}); we
 *     render that outcome AT THE MOMENT, exactly as returned — admitted, queued,
 *     ignored, rejected or expired — never softened, never invented. This is the
 *     ONLY place an activation outcome is shown, because `Admission` is on no
 *     operator GET (design §5/§6: `/runtime` carries no `admissions` field).
 *
 *  2. CURRENT RUNS — the live Runs from `runtime.current.runs`, each with Finish
 *     and Cancel (`POST …/runs/{id}/finish|cancel`). These are the runs the
 *     projection actually holds right now; there is NO history list, and in
 *     particular NO "expired: missed_window" row — that reason is a server-side
 *     Admission on no GET, so surfacing it would be inventing state the operator
 *     surface cannot truthfully know (design §5/§6, R2).
 *
 *  3. WHY — for a selected Frame, its `contributions` ranked by the total
 *     precedence order (priority, root_order, admission_order) via the shared
 *     read `rankedContributions` (primitive #4, join.js) — the same ranking the
 *     Now-showing facet renders, in one place.
 *
 * Activate/finish/cancel all wrap the shared `useMutate()` hook (primitive #7)
 * so Plane A — and therefore the Runs list and the "why" panel — refresh exactly
 * once after each write.
 *
 * @param {{snapshot: object|null}} props
 */
function RunControl({ snapshot }) {
  const definitions = snapshot?.runtime?.definitions ?? {};
  const scenes = useMemo(() => Object.values(definitions), [definitions]);
  // The LIVE Runs the projection holds right now: `runtime.current.runs` carries
  // ALL runs (including completed/cancelled ones, whose phase has advanced), so
  // filter to the live phases — body/outro — the SAME "live" definition the
  // delete guard uses (runtime.py:233; RunView.phase, runtime.py:178). Only a
  // live Run is Finish/Cancel-able, and a cancelled/completed Run drops off here.
  // This is the ONLY Run list — no history, so no missed-window row can appear.
  const runs = useMemo(() => {
    const all = snapshot?.runtime?.current?.runs ?? [];
    return all.filter((run) => run.phase === "body" || run.phase === "outro");
  }, [snapshot]);
  const frames = snapshot?.inventory?.frames ?? [];

  const mutate = useMutate();

  // Plane B: the activation draft + the SYNCHRONOUS outcome of the last activate.
  const [sceneId, setSceneId] = useState("");
  const [activationId, setActivationId] = useState("");
  const [priority, setPriority] = useState(0);
  // The synchronous Admission of the last activation, shown at the moment and
  // held in component state ONLY (never read back from a GET). Null until the
  // operator activates something — so no activation outcome is shown on load.
  const [outcome, setOutcome] = useState(
    /** @type {{status: string, reason: string|null}|null} */ (null),
  );
  const [activating, setActivating] = useState(false);

  // The Frame whose "why" is shown; defaults to none until the operator picks.
  const [whyFrame, setWhyFrame] = useState("");

  const activateValid =
    !activating && sceneId !== "" && activationId.trim() !== "";

  const activate = useCallback(async () => {
    const id = activationId.trim();
    setActivating(true);
    setOutcome(null);
    try {
      const result = await mutate(() =>
        apiWrite("/v1/operator/activations", {
          method: "POST",
          body: {
            scene_id: sceneId,
            activation_id: id,
            priority: Number(priority),
          },
        }),
      );
      // Show the SYNCHRONOUS server outcome truthfully. On a 2xx the body is the
      // Admission {activation_id, status, run_id, reason}; on a non-2xx we report
      // the transport failure honestly rather than claim an activation status.
      if (result.ok && result.data && typeof result.data.status === "string") {
        setOutcome({
          status: result.data.status,
          reason: result.data.reason ?? null,
        });
      } else {
        setOutcome({
          status: "not accepted",
          reason: result.error ?? `HTTP ${result.status}`,
        });
      }
    } catch {
      setOutcome({ status: "not accepted", reason: "the request did not complete" });
    } finally {
      setActivating(false);
    }
  }, [sceneId, activationId, priority, mutate]);

  const finishRun = useCallback(
    (runId) =>
      mutate(() =>
        apiWrite(`/v1/operator/runs/${encodeURIComponent(runId)}/finish`, {
          method: "POST",
        }),
      ),
    [mutate],
  );

  const cancelRun = useCallback(
    (runId) =>
      mutate(() =>
        apiWrite(`/v1/operator/runs/${encodeURIComponent(runId)}/cancel`, {
          method: "POST",
        }),
      ),
    [mutate],
  );

  const why = whyFrame === "" ? [] : rankedContributions(snapshot?.runtime, whyFrame);

  return (
    <div className="run-control">
      <form
        className="run-control__activate"
        role="form"
        aria-label="Activate a Scene"
        onSubmit={(event) => {
          event.preventDefault();
          if (activateValid) {
            activate();
          }
        }}
      >
        <label className="run-control__field">
          Scene to activate
          <select
            className="run-control__scene"
            aria-label="Scene to activate"
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

        <label className="run-control__field">
          Activation ID
          <input
            type="text"
            className="run-control__activation-id"
            aria-label="Activation ID"
            value={activationId}
            onChange={(event) => setActivationId(event.target.value)}
          />
        </label>

        <label className="run-control__field">
          Priority
          <input
            type="number"
            className="run-control__priority"
            aria-label="Activation priority"
            value={priority}
            onChange={(event) => setPriority(event.target.value)}
          />
        </label>

        <button
          type="submit"
          className="run-control__activate-button"
          disabled={!activateValid}
        >
          Activate now
        </button>
      </form>

      {/* The SYNCHRONOUS activation outcome — shown at the moment, exactly as the
          server returned it, and only after an activation (null until then). This
          is the sole activation-outcome surface; the calendar/Runs area renders no
          history and no missed_window row (design §5/§6). */}
      {outcome !== null ? (
        <p className="run-control__outcome" role="status" aria-label="Activation outcome">
          {outcome.reason
            ? `Activation ${outcome.status}: ${outcome.reason}`
            : `Activation ${outcome.status}`}
        </p>
      ) : null}

      {/* Current live Runs (runtime.current.runs) — Finish/Cancel each. */}
      {runs.length === 0 ? (
        <p className="run-control__empty">No live Runs.</p>
      ) : (
        <ul className="run-control__runs" role="list">
          {runs.map((run) => (
            <li
              key={run.run_id}
              className="run-control__run"
              aria-label={`Run ${run.run_id}`}
            >
              <span className="run-control__run-scene">{`Scene ${run.scene_id}`}</span>
              <span className="run-control__run-phase">{`Phase ${run.phase}`}</span>
              <button
                type="button"
                className="run-control__finish"
                aria-label={`Finish run ${run.run_id}`}
                onClick={() => finishRun(run.run_id)}
              >
                Finish
              </button>
              <button
                type="button"
                className="run-control__cancel"
                aria-label={`Cancel run ${run.run_id}`}
                onClick={() => cancelRun(run.run_id)}
              >
                Cancel
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* The "why" panel: pick a Frame, see its contributions ranked by the total
          precedence order (priority, root_order, admission_order) via the shared
          primitive #4 read — deterministic, winner first. */}
      <div className="run-control__why" role="group" aria-label="Why">
        <label className="run-control__field">
          Frame for why
          <select
            className="run-control__why-frame"
            aria-label="Frame for why"
            value={whyFrame}
            onChange={(event) => setWhyFrame(event.target.value)}
          >
            <option value="">Choose a Frame</option>
            {frames.map((frame) => (
              <option key={frame.id} value={frame.id}>
                {frame.id}
              </option>
            ))}
          </select>
        </label>
        {whyFrame === "" ? null : why.length === 0 ? (
          <p className="run-control__empty">No contributions target this frame.</p>
        ) : (
          <ol className="run-control__why-list" aria-label="Contribution precedence">
            {why.map((intent, index) => (
              <li
                key={`${intent.run_id}:${index}`}
                className="run-control__why-row"
              >
                {`${intent.scene_id} — priority ${intent.priority}, ` +
                  `root order ${intent.root_order}, admission ${intent.admission_order}`}
              </li>
            ))}
          </ol>
        )}
      </div>
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
