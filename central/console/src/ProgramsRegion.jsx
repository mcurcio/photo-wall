import React, { useCallback, useMemo, useRef, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import {
  draftId,
  MAX_WINDOWS,
  planWindows,
  programProblems,
  timeZoneName,
  toEpochSeconds,
  windowProblems,
} from "./authoring.js";
import { useConfirm } from "./ConfirmAction.jsx";
import { UNKNOWN_MESSAGE } from "./equipmentApi.js";
import { Field, IdentityFields, ProblemSummary, useProblems } from "./Field.jsx";
import { ScenePicker } from "./ScenePicker.jsx";
import { programState, windowLabel } from "./showState.js";
import { useMutate } from "./useMutate.js";

/**
 * The Programs region (moved out of Showrunner.jsx, pass 2 slice 3 §3).
 *
 * @param {{snapshot: object|null}} props
 */
export function ProgramsRegion({ snapshot }) {
  const regionRef = useRef(/** @type {HTMLElement|null} */ (null));
  // After a removal, or when its opener is gone, the Programs region takes focus.
  const focusRegion = () => regionRef.current?.focus();
  const confirm = useConfirm(focusRegion, focusRegion);
  return (
    <section
      ref={regionRef}
      tabIndex={-1}
      className="showrunner__region"
      role="region"
      aria-label="Programs"
    >
      <h2 className="showrunner__region-title">Programs</h2>
      {/* A Program binds a Scene to a SINGLE time window with a priority
          (PUT …/programs/{id}). The optional helper creates N SEPARATE
          windows — N individual, independently-stored Programs — and is never
          described as a recurring rule: central stores no recurrence model
          (design R2, Q2), so the UI implies none. */}
      <ProgramScheduling snapshot={snapshot} confirm={confirm} />
      {confirm.confirmation("program-scheduling__status-line")}
    </section>
  );
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

// Weekdays in display order, each with its `Date.getDay()` index.
const WEEKDAYS = [
  ["Monday", 1],
  ["Tuesday", 2],
  ["Wednesday", 3],
  ["Thursday", 4],
  ["Friday", 5],
  ["Saturday", 6],
  ["Sunday", 0],
];

const EVERY_DAY = [true, true, true, true, true, true, true];

/**
 * The Programs region body (Bead 15; pass 2 slice 3 §5–§7, §9).
 *
 * A Program binds a Scene to a SINGLE time window with a priority (design J4).
 * The operator names it and the id is derived (authoring.js). The form saves
 * one Program via `PUT /v1/operator/programs/{id}`; the helper creates N
 * SEPARATE windows in one action — N independent Programs `<id>-<n>`, window 1
 * the entered one and each later one on the next ticked weekday at the same
 * local clock times — each a real single-window `PUT`, managed and removed one
 * by one. Nothing here is a recurring rule: central stores none (design R2/Q2).
 * Times are entered and shown in the browser's time zone, which is named.
 *
 * All writes wrap the shared `useMutate()` hook (primitive #7), so Plane A —
 * and the Programs list below — refreshes exactly once after a write.
 *
 * @param {{snapshot: object|null, confirm: ReturnType<typeof useConfirm>}} props
 */
function ProgramScheduling({ snapshot, confirm }) {
  const definitions = snapshot?.runtime?.definitions ?? {};
  // Stored Programs are the runtime programs map, keyed by program_id
  // (central/app.py -> runtime.export_state()["programs"]).
  const programsMap = snapshot?.runtime?.programs ?? {};
  const programs = useMemo(() => Object.values(programsMap), [programsMap]);
  const programIds = useMemo(() => new Set(Object.keys(programsMap)), [programsMap]);
  const now = snapshot?.runtime?.current?.now;

  const mutate = useMutate();

  // Plane B: component-local scheduling draft.
  const [name, setName] = useState("");
  const [idOverride, setIdOverride] = useState(/** @type {string|null} */ (null));
  const [sceneId, setSceneId] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [priority, setPriority] = useState(/** @type {string|number} */ (0));
  // The helper: how many SEPARATE windows, and on which weekdays.
  const [windowCount, setWindowCount] = useState(/** @type {string|number} */ (3));
  const [weekdays, setWeekdays] = useState(EVERY_DAY);
  // Which action the reasons beside the fields describe: the last one tried.
  const [action, setAction] = useState(/** @type {"single"|"windows"} */ ("single"));
  const [status, setStatus] = useState(/** @type {string|null} */ (null));
  const [saving, setSaving] = useState(false);

  const draft = { name, idOverride, sceneId, start, end, priority, weekdays, count: windowCount };
  const single = programProblems(draft, programIds, now);
  const windows = windowProblems(draft, programIds, now);
  const problems = useProblems(
    action === "windows"
      ? windows
      : [...single, ...windows.filter((p) => p.field === "count" || p.field === "weekdays")],
  );
  const count = Number(windowCount);
  const countShown = Number.isInteger(count) && count >= 1 && count <= MAX_WINDOWS;

  const field = (setter, key) => (value) => {
    setter(value);
    problems.touch(key);
  };

  // A successful save clears the form, so the saved Program never reads as a
  // collision with itself (§5).
  const clear = () => {
    setName("");
    setIdOverride(null);
    setSceneId("");
    setStart("");
    setEnd("");
    setPriority(0);
    setWindowCount(3);
    setWeekdays(EVERY_DAY);
    setAction("single");
    problems.reset();
  };

  const put = (programId, startsAt, endsAt) =>
    apiWrite(`/v1/operator/programs/${encodeURIComponent(programId)}`, {
      method: "PUT",
      body: buildProgram({ programId, sceneId, startsAt, endsAt, priority: Number(priority) }),
    });

  const saveProgram = async () => {
    setAction("single");
    if (saving || !problems.check(single)) {
      return;
    }
    const id = draftId(draft);
    setSaving(true);
    setStatus(null);
    try {
      const result = await mutate(() => put(id, toEpochSeconds(start), toEpochSeconds(end)));
      if (result.ok) {
        clear();
        setStatus(`Scheduled Program ${id}.`);
      } else {
        setStatus(`Could not schedule Program: ${result.error ?? result.status}.`);
      }
    } catch {
      setStatus("Could not schedule Program: the request did not complete.");
    } finally {
      setSaving(false);
    }
  };

  // The helper: `count` SEPARATE Programs in one action, each a real stored
  // single-window Program `<id>-<n>`. A partial failure names the ids that
  // were not created.
  const addSeparateWindows = async () => {
    setAction("windows");
    if (saving || !problems.check(windows)) {
      return;
    }
    const id = draftId(draft);
    const planned = planWindows({ start, end, weekdays, count });
    setSaving(true);
    setStatus(null);
    const results = await mutate(() =>
      Promise.all(
        planned.map(({ startsAt, endsAt }, index) =>
          put(`${id}-${index + 1}`, startsAt, endsAt).then(
            (result) => result.ok,
            () => false,
          ),
        ),
      ),
    );
    const failed = planned
      .map((_window, index) => `${id}-${index + 1}`)
      .filter((_windowId, index) => !results[index]);
    if (failed.length === 0) {
      clear();
      setStatus(`Created ${planned.length} separate Programs.`);
    } else {
      setStatus(
        `Created ${planned.length - failed.length} of ${planned.length} separate Programs. ` +
          `Not created: ${failed.join(", ")}.`,
      );
    }
    setSaving(false);
  };

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

  // Removing a RUNNING Program ends its Run, so it is confirmed (§9).
  const onRemove = (event, program) => {
    if (programState(snapshot, program.program_id)?.state === "running") {
      confirm.open(event, removeRunningRequest(program));
    } else {
      removeProgram(program.program_id);
    }
  };

  const past = programs.filter((program) => program.ends_at <= now);
  const current = programs.filter((program) => !(program.ends_at <= now));

  const dateField = (key, label, value, setter) => (
    <Field id={problems.idFor(key)} label={label} reason={problems.reasonFor(key)}>
      {(props) => (
        <input
          {...props}
          type="datetime-local"
          value={value}
          onChange={(event) => field(setter, key)(event.target.value)}
        />
      )}
    </Field>
  );

  return (
    <div className="program-scheduling">
      <form
        className="program-scheduling__form"
        role="form"
        aria-label="Schedule a Program"
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          saveProgram();
        }}
      >
        <ProblemSummary summary={problems.summary} label="Program problems" />
        <IdentityFields
          kind="Program"
          name={name}
          idOverride={idOverride}
          onName={setName}
          onIdOverride={setIdOverride}
          problems={problems}
        />
        <ScenePicker
          id={problems.idFor("scene")}
          label="Scene"
          reason={problems.reasonFor("scene")}
          definitions={definitions}
          value={sceneId}
          onChange={field(setSceneId, "scene")}
        />
        <p className="program-scheduling__zone">{`Times in ${timeZoneName()}`}</p>
        {dateField("start", "Window start", start, setStart)}
        {dateField("end", "Window end", end, setEnd)}
        <Field id={problems.idFor("priority")} label="Priority" reason={problems.reasonFor("priority")}>
          {(props) => (
            <input
              {...props}
              type="number"
              step="1"
              value={priority}
              onChange={(event) => field(setPriority, "priority")(event.target.value)}
            />
          )}
        </Field>

        <button type="submit" className="program-scheduling__save" disabled={saving}>
          Schedule Program
        </button>

        {/* Optional helper (design Q2): create N SEPARATE windows at once. Each is
            a real, independently-stored single-window Program — N discrete
            Programs to manage individually, never a rule that repeats itself. */}
        <fieldset className="program-scheduling__multi" aria-label="Create separate windows">
          <legend>Create separate windows</legend>
          <p className="program-scheduling__multi-note">
            {countShown
              ? `Creates ${count} separate windows — ${count} individual Programs, each stored ` +
                "on its own and removed one by one below. Window 1 is the one above; each " +
                "later one falls on the next ticked day at the same local times."
              : "Creates separate windows — individual Programs, each stored on its own and " +
                "removed one by one below."}
          </p>
          <fieldset
            id={problems.idFor("weekdays")}
            className="program-scheduling__weekdays"
            aria-label="Repeat on"
            aria-describedby={
              problems.reasonFor("weekdays") !== null ? `${problems.idFor("weekdays")}-reason` : undefined
            }
          >
            <legend>Repeat on</legend>
            {WEEKDAYS.map(([day, index]) => (
              <label key={day} className="program-scheduling__weekday">
                <input
                  type="checkbox"
                  checked={weekdays[index]}
                  onChange={(event) => {
                    const next = [...weekdays];
                    next[index] = event.target.checked;
                    field(setWeekdays, "weekdays")(next);
                  }}
                />
                {day}
              </label>
            ))}
            {problems.reasonFor("weekdays") !== null && (
              <p id={`${problems.idFor("weekdays")}-reason`} className="field__reason">
                {problems.reasonFor("weekdays")}
              </p>
            )}
          </fieldset>
          <Field
            id={problems.idFor("count")}
            label="Number of windows"
            reason={problems.reasonFor("count")}
          >
            {(props) => (
              <input
                {...props}
                type="number"
                min="1"
                max={MAX_WINDOWS}
                className="program-scheduling__count"
                value={windowCount}
                onChange={(event) => field(setWindowCount, "count")(event.target.value)}
              />
            )}
          </Field>
          <button
            type="button"
            className="program-scheduling__multi-add"
            aria-label="Add separate windows"
            disabled={saving}
            onClick={addSeparateWindows}
          >
            {countShown ? `Add ${count} separate windows` : "Add separate windows"}
          </button>
        </fieldset>
      </form>

      {status !== null ? (
        <p className="program-scheduling__status" role="status">
          {status}
        </p>
      ) : null}

      {programs.length === 0 ? (
        <p className="program-scheduling__empty">No Programs yet.</p>
      ) : (
        <>
          {/* Every stored Program by its program_id, each one discrete window with
              its display state (showState.js); past windows sit under a closed
              "Past (N)" disclosure. */}
          <ProgramList programs={current} snapshot={snapshot} onRemove={onRemove} />
          {past.length > 0 && (
            <details className="program-scheduling__past">
              <summary>{`Past (${past.length})`}</summary>
              <ProgramList programs={past} snapshot={snapshot} onRemove={onRemove} />
            </details>
          )}
        </>
      )}
    </div>
  );
}

/**
 * Stored Programs as records: the Scene, the window in local time, the
 * priority, and the display state with its hint (§9).
 */
function ProgramList({ programs, snapshot, onRemove }) {
  return (
    <ul className="program-scheduling__programs" role="list">
      {programs.map((program) => {
        const state = programState(snapshot, program.program_id);
        return (
          <li
            key={program.program_id}
            className="program-scheduling__program"
            aria-label={`Program ${program.program_id}`}
          >
            <p className="program-scheduling__program-id-text">{program.program_id}</p>
            <dl className="record">
              <dt>Scene</dt>
              <dd className="program-scheduling__program-scene">{`Scene ${program.scene_id}`}</dd>
              <dt>Window</dt>
              <dd className="program-scheduling__program-window">{windowLabel(program)}</dd>
              <dt>Priority</dt>
              <dd className="program-scheduling__program-priority">{`Priority ${program.priority}`}</dd>
              <dt>State</dt>
              <dd className={`program-scheduling__program-state health--${state.severity}`}>
                {state.label}
                {state.hint !== null && (
                  <span className="program-scheduling__program-hint">{` ${state.hint}`}</span>
                )}
              </dd>
            </dl>
            <div className="record__actions">
              <button
                type="button"
                className="program-scheduling__remove"
                aria-label={`Remove program ${program.program_id}`}
                onClick={(event) => onRemove(event, program)}
              >
                Remove
              </button>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

/** Remove a running Program: its Run is asked to finish now (central/runtime.py `remove_program`). */
function removeRunningRequest(program) {
  return {
    key: `remove-program:${program.program_id}`,
    title: `Remove program ${program.program_id}?`,
    confirmLabel: "Confirm remove",
    body: (
      <p>
        {`It is running now. Its Run of ${program.scene_id} is asked to finish: it ends at the ` +
          "end of its current cycle, after any outro. Later windows are separate Programs and stay."}
      </p>
    ),
    run: async () => {
      const result = await apiWrite(`/v1/operator/programs/${encodeURIComponent(program.program_id)}`, {
        method: "DELETE",
      });
      if (result.ok) {
        return { state: "done", message: `Program ${program.program_id} removed.` };
      }
      if (result.status >= 500) {
        return { state: "unknown", message: UNKNOWN_MESSAGE };
      }
      return { state: "refused", message: `Not removed: ${result.error ?? result.status}.` };
    },
  };
}
