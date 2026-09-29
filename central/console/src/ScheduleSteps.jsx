import React from "react";

import { draftId, idFromName, MAX_WINDOWS, planWindows, timeZoneName } from "./authoring.js";
import { Field, IdField, idNeeded, NameField, PriorityField } from "./Field.jsx";
import { Advanced } from "./flow/Advanced.jsx";
import { CheckAnswers, NotChosen } from "./flow/CheckAnswers.jsx";
import { OfferedScene } from "./flow/InstanceNotice.jsx";
import { effectiveProgramTimes, separateWindows } from "./scheduleFlowModel.js";
import { ScenePicker } from "./ScenePicker.jsx";
import { windowLabel } from "./showState.js";

/**
 * The Schedule flow's step views (flow design §7 J6): views over the draft that
 * ProgramsRegion.jsx owns. They hold no draft state; each renders its fields through
 * the shared pickers and Field.jsx, so labels and accessible names are the ones the
 * single Programs form had (rule 3): "Scene", "Window start", "Window end", the
 * "Create separate windows" group with "Repeat on" and "Number of windows",
 * "Program name", "Id" and "Priority".
 *
 * Every view takes `{value, patch, problems}`: the draft value, the draft's patch, and
 * the flow's `useProblems`. Views with an Advanced disclosure also take
 * `advanced: {open, onToggle}`.
 *
 * @typedef {import("./scheduleFlowModel.js").ProgramDraft} ProgramDraft
 * @typedef {ReturnType<typeof import("./Field.jsx").useProblems>} Problems
 * @typedef {{value: ProgramDraft, patch: (partial: Partial<ProgramDraft>) => void,
 *            problems: Problems}} StepProps
 */

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

/** The ticked weekdays in words: "every day", "Mon, Tue, Wed" or "no day". */
function weekdayWords(weekdays) {
  const ticked = WEEKDAYS.filter(([, index]) => weekdays[index]).map(([day]) => day.slice(0, 3));
  if (ticked.length === WEEKDAYS.length) {
    return "every day";
  }
  return ticked.length === 0 ? "no day" : ticked.join(", ");
}

/** A usable "Number of windows", or null. */
function windowCount(value) {
  const count = Number(value.count);
  return Number.isInteger(count) && count >= 1 && count <= MAX_WINDOWS ? count : null;
}

/** A `datetime-local` value in words, in the browser's time zone. */
function localWords(local) {
  return new Date(local).toLocaleString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * "Times in Europe/London": the zone every entered and shown time uses (slice 3 §6).
 * If the browser and the wall are in different zones, this is the only warning.
 */
export function TimeZoneNote() {
  return <p className="program-scheduling__zone">{`Times in ${timeZoneName()}`}</p>;
}

/**
 * Step 1, Scene: the Scene to schedule (required), prefilled from the Scene the
 * operator just saved or picked. `offered` is a Scene handed over ("Schedule it")
 * while an unsaved draft already names another: the draft is kept, and a button
 * offers the handed-over Scene instead.
 *
 * @param {StepProps & {definitions: Record<string, object>, offered: string|null,
 *          onUseOffered: () => void}} props
 */
export function SceneStep({ value, patch, problems, definitions, offered, onUseOffered }) {
  return (
    <>
      <OfferedScene
        offered={offered}
        current={value.sceneId}
        drafts="schedules"
        action="Schedule"
        onTake={onUseOffered}
      />
      <ScenePicker
        id={problems.idFor("scene")}
        label="Scene"
        reason={problems.reasonFor("scene")}
        definitions={definitions}
        value={value.sceneId}
        onChange={(sceneId) => {
          patch({ sceneId });
          problems.touch("scene");
        }}
      />
    </>
  );
}

/** Advanced's summary on When: one window, or how many and on which days. */
function windowsSummary(value) {
  if (!separateWindows(value)) {
    return "One window";
  }
  const count = windowCount(value);
  return count === null
    ? `Number of windows: ${value.count}`
    : `${count} separate windows, on ${weekdayWords(value.weekdays)}`;
}

/**
 * Step 2, When: "Window start" and "Window end" (required), in the named time zone.
 * Under Advanced, the separate-windows helper (design Q2): "Repeat on" (every day) and
 * "Number of windows" (1: one Program). More than one creates that many SEPARATE
 * Programs `<id>-<n>`, window 1 the one entered and each later one on the next ticked
 * day at the same local clock times; never a rule that repeats itself.
 *
 * @param {StepProps & {advanced: {open: boolean, onToggle: () => void}}} props
 */
export function WhenStep({ value, patch, problems, advanced, editableWindows = true }) {
  const savedOccurrences = repeatedOccurrenceCopy(value);
  const date = (key, label) => (
    <Field id={problems.idFor(key)} label={label} reason={problems.reasonFor(key)}>
      {(props) => (
        <input
          {...props}
          type="datetime-local"
          step="any"
          value={value[key]}
          onChange={(event) => {
            patch({ [key]: event.target.value });
            problems.touch(key);
          }}
        />
      )}
    </Field>
  );
  return (
    <>
      <TimeZoneNote />
      {savedOccurrences.length > 0 && <p className="field__hint" role="note">{savedOccurrences.join(" · ")}. Leaving these fields unchanged keeps the saved occurrence; changing a time uses the normal local-time interpretation.</p>}
      {date("start", "Window start")}
      {date("end", "Window end")}
      {editableWindows && (
        <Advanced summary={windowsSummary(value)} open={advanced.open} onToggle={advanced.onToggle}>
          <SeparateWindows value={value} patch={patch} problems={problems} />
        </Advanced>
      )}
    </>
  );
}

/** The separate-windows helper's fields: "Repeat on" and "Number of windows". */
function SeparateWindows({ value, patch, problems }) {
  const count = windowCount(value);
  const weekdaysReason = problems.reasonFor("weekdays");
  const weekdaysReasonId = `${problems.idFor("weekdays")}-reason`;
  return (
    <fieldset className="program-scheduling__multi" aria-label="Create separate windows">
      <legend>Create separate windows</legend>
      <p className="program-scheduling__multi-note">
        {count !== null && count > 1
          ? `Creates ${count} separate windows — ${count} individual Programs, each stored ` +
            "on its own and removed one by one. Window 1 is the one above; each later one " +
            "falls on the next ticked day at the same local times."
          : "With more than 1, creates separate windows — individual Programs, each stored on " +
            "its own and removed one by one. Window 1 is the one above; each later one falls " +
            "on the next ticked day at the same local times."}
      </p>
      <fieldset
        id={problems.idFor("weekdays")}
        className="program-scheduling__weekdays"
        aria-label="Repeat on"
        aria-describedby={weekdaysReason !== null ? weekdaysReasonId : undefined}
      >
        <legend>Repeat on</legend>
        {WEEKDAYS.map(([day, index]) => (
          <label key={day} className="program-scheduling__weekday">
            <input
              type="checkbox"
              checked={value.weekdays[index]}
              onChange={(event) => {
                const weekdays = [...value.weekdays];
                weekdays[index] = event.target.checked;
                patch({ weekdays });
                problems.touch("weekdays");
              }}
            />
            {day}
          </label>
        ))}
        {weekdaysReason !== null && (
          <p id={weekdaysReasonId} className="field__reason">
            {weekdaysReason}
          </p>
        )}
      </fieldset>
      <Field id={problems.idFor("count")} label="Number of windows" reason={problems.reasonFor("count")}>
        {(props) => (
          <input
            {...props}
            type="number"
            min="1"
            max={MAX_WINDOWS}
            className="program-scheduling__count"
            value={value.count}
            onChange={(event) => {
              patch({ count: event.target.value });
              problems.touch("count");
            }}
          />
        )}
      </Field>
    </fieldset>
  );
}

/** The windows a separate-windows draft creates, each in local time. */
function PlannedWindows({ value, count }) {
  const windows = planWindows({ ...value, count });
  return (
    <ol className="program-flow__windows">
      {windows.map((window, index) => (
        <li key={window.startsAt}>
          {`${index + 1}: ${windowLabel({ starts_at: window.startsAt, ends_at: window.endsAt })}`}
        </li>
      ))}
    </ol>
  );
}

/**
 * Step 3, Review: a check-answers list of every value, the advanced ones included
 * (Number of windows, Repeat on, Priority, Id), each with "Change"; then "Program
 * name" and, under Advanced, "Priority" (default 0) and the "Id" (derived from the
 * name until the operator types one, slice 3 §5).
 *
 * The name is asked here, last, as the Scene flow asks its name: the id derives from
 * it, and whether that id (or, for separate windows, each `<id>-<n>`) is free and short
 * enough depends on the number of windows When decided.
 *
 * @param {StepProps & {advanced: {open: boolean, onToggle: () => void},
 *          onChange: (field: string) => void}} props
 */
export function ReviewStep({ value, patch, problems, advanced, onChange, editableWindows = true, editing = false }) {
  const savedOccurrences = repeatedOccurrenceCopy(value);
  const separate = editableWindows && separateWindows(value);
  const count = windowCount(value);
  const id = draftId(value);
  const derived = idFromName(value.name);
  const plannable = separate && count !== null && value.start !== "" && value.end !== "" &&
    value.weekdays.some(Boolean);
  let windows = "1 (one Program)";
  if (plannable) {
    windows = <PlannedWindows value={value} count={count} />;
  } else if (separate) {
    windows = String(value.count);
  }
  const rows = [
    { label: "Scene", field: "scene", value: value.sceneId === "" ? <NotChosen /> : `Scene ${value.sceneId}` },
    { label: "Window start", field: "start", value: value.start === "" ? <NotChosen /> : localWords(value.start) },
    { label: "Window end", field: "end", value: value.end === "" ? <NotChosen /> : localWords(value.end) },
    ...(editableWindows ? [{ label: "Number of windows", field: "count", value: windows }] : []),
    ...(editableWindows ? [{
      label: "Repeat on",
      field: "weekdays",
      value: separate ? weekdayWords(value.weekdays) : "Not used for one window",
    }] : []),
    { label: "Priority", field: "priority", value: String(value.priority) },
    editing ? { label: "Program id", value: id } : {
      label: "Id",
      field: "id",
      value: !id ? <NotChosen /> : separate && count !== null ? `${id}-1 … ${id}-${count}` : id,
    },
  ];
  return (
    <>
      <TimeZoneNote />
      {savedOccurrences.length > 0 && <p className="field__hint" role="note">{savedOccurrences.join(" · ")}. The saved occurrence is kept only while the corresponding time remains unchanged.</p>}
      <CheckAnswers rows={rows} onChange={onChange} />
      {editing ? <p className="field__hint">Program id: {value.idOverride}</p> : <NameField
        kind="Program"
        name={value.name}
        idShown={value.idOverride !== null || idNeeded(value.name)}
        onName={(name) => patch({ name })}
        onChangeId={() => {
          patch({ idOverride: derived });
          onChange("id");
        }}
        problems={problems}
      />}
      <Advanced
        summary={`Priority: ${value.priority} · Id: ${id || "none yet"}`}
        open={advanced.open}
        lockedOpen={idNeeded(value.name)}
        onToggle={advanced.onToggle}
      >
        <PriorityField
          label="Priority"
          problems={problems}
          value={value.priority}
          onChange={(priority) => {
            patch({ priority });
            problems.touch("priority");
          }}
        />
        {!editing && <IdField
          value={value.idOverride ?? derived ?? ""}
          onIdOverride={(idOverride) => patch({ idOverride })}
          problems={problems}
        />}
      </Advanced>
    </>
  );
}

function repeatedOccurrenceCopy(value) {
  if (!value.expected) return [];
  const times = effectiveProgramTimes(value);
  const format = (seconds) => {
    const date = new Date(seconds * 1000);
    const clock = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit", timeZoneName: "short" }).format(date);
    const offset = new Intl.DateTimeFormat(undefined, { timeZoneName: "longOffset" })
      .formatToParts(date).find((part) => part.type === "timeZoneName")?.value ?? "local time";
    return `${clock} (${offset})`;
  };
  return [
    times.retainedStart ? `Saved start occurrence: ${format(value.expected.starts_at)}` : null,
    times.retainedEnd ? `Saved end occurrence: ${format(value.expected.ends_at)}` : null,
  ].filter(Boolean);
}
