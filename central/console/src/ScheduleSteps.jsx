import React from "react";

import { Field } from "./Field.jsx";
import { Advanced } from "./flow/Advanced.jsx";
import { CheckAnswers, NotChosen } from "./flow/CheckAnswers.jsx";
import { OfferedScene } from "./flow/InstanceNotice.jsx";
import { buildProgram, DAY_KEYS, PRIORITIES, TIME_ATS } from "./scheduleFlowModel.js";
import { ScenePicker } from "./ScenePicker.jsx";
import { whenWords, windowWords } from "./timeWords.js";

/**
 * The Schedule flow's step views (flow design §7 J6; Power and the week): views over the draft
 * that ProgramsRegion.jsx owns. They hold no draft state. A Program repeats every week: its
 * days, a start and an end on the house clock ("17:00"), an end at or before the start being
 * the next day (shown, never asked, #76). Central works out every window in the house's time
 * zone; nothing here reads the browser's zone.
 *
 * Every view takes `{value, patch, problems}`: the draft value, the draft's patch, and the
 * flow's `useProblems`. Views with an Advanced disclosure also take `advanced: {open, onToggle}`.
 *
 * @typedef {import("./scheduleFlowModel.js").ProgramDraft} ProgramDraft
 * @typedef {ReturnType<typeof import("./Field.jsx").useProblems>} Problems
 * @typedef {{value: ProgramDraft, patch: (partial: Partial<ProgramDraft>) => void,
 *            problems: Problems}} StepProps
 */

const DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

/** "Times are on the house clock": the one note on where the times live. */
export function TimeZoneNote() {
  return <p className="program-scheduling__zone">Times are on the house clock; Central works out each window.</p>;
}

/** The draft's When in words, once its days and times are filled: "Mon–Fri, 17:00–23:00". */
function draftWhenWords(value) {
  const unset = (key) => value[`${key}At`] === "clock" && value[key] === "";
  if (unset("start") || unset("end") || !value.days.some(Boolean)) return null;
  return whenWords(buildProgram(value).when);
}

/**
 * Step 1, Scene: the Scene to schedule (required), prefilled from the Scene the operator just
 * saved or picked. `offered` is a Scene handed over ("Schedule it") while an unsaved draft
 * already names another: the draft is kept, and a button offers the handed-over Scene instead.
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

/**
 * Step 2, When: "Days" (every day ticked by default), "Starts" and "Ends": each a clock time on
 * the house clock, or sunrise or sunset plus or minus minutes (#75; Central refuses a sun time
 * while the house has no location, and the refusal returns here). An end at or before the start
 * reads "(ends next day)" (#76).
 *
 * @param {StepProps} props
 */
export function WhenStep({ value, patch, problems }) {
  const daysReason = problems.reasonFor("days");
  const daysReasonId = `${problems.idFor("days")}-reason`;
  const words = draftWhenWords(value);
  // A start or end: where it sits (a clock time, or sunrise or sunset, #75), then its clock time
  // or its minutes from the sun.
  const time = (key, label) => {
    const at = value[`${key}At`];
    const offset = value[`${key}Offset`];
    // Before the sun: a negative offset, or −0 (Before chosen at 0 minutes).
    const before = offset < 0 || Object.is(offset, -0);
    return (
      <div className="program-scheduling__time">
        <Field id={`${problems.idFor(key)}-at`} label={`${label} at`}>
          {(props) => (
            <select
              {...props}
              value={at}
              onChange={(event) => {
                patch({ [`${key}At`]: event.target.value });
                problems.touch(key);
              }}
            >
              {TIME_ATS.map(([choice, words]) => <option key={choice} value={choice}>{words}</option>)}
            </select>
          )}
        </Field>
        {at === "clock" ? (
          <Field id={problems.idFor(key)} label={label} reason={problems.reasonFor(key)}>
            {(props) => (
              <input
                {...props}
                type="time"
                value={value[key]}
                onChange={(event) => {
                  patch({ [key]: event.target.value });
                  problems.touch(key);
                }}
              />
            )}
          </Field>
        ) : (
          // The side is chosen, not typed as a sign: a typed "−" was lost on the way.
          <>
            <Field id={`${problems.idFor(key)}-side`} label={`${label}: before or after ${at}`}>
              {(props) => (
                <select
                  {...props}
                  value={before ? "before" : "after"}
                  onChange={(event) => {
                    patch({ [`${key}Offset`]: (event.target.value === "before" ? -1 : 1) * Math.abs(offset) });
                    problems.touch(key);
                  }}
                >
                  <option value="before">Before</option>
                  <option value="after">After</option>
                </select>
              )}
            </Field>
            <Field id={problems.idFor(key)} label={`${label}: minutes ${before ? "before" : "after"} ${at}`}
              reason={problems.reasonFor(key)} hint="0 to 180.">
              {(props) => (
                <input
                  {...props}
                  type="number"
                  min={0}
                  max={180}
                  step={5}
                  value={Math.abs(offset)}
                  onChange={(event) => {
                    const minutes = Math.min(180, Math.abs(Math.round(Number(event.target.value) || 0)));
                    patch({ [`${key}Offset`]: before ? -minutes : minutes });
                    problems.touch(key);
                  }}
                />
              )}
            </Field>
          </>
        )}
      </div>
    );
  };
  return (
    <>
      <TimeZoneNote />
      <fieldset
        id={problems.idFor("days")}
        className="program-scheduling__weekdays"
        aria-label="Days"
        aria-describedby={daysReason !== null ? daysReasonId : undefined}
      >
        <legend>Days</legend>
        {DAY_KEYS.map((day, index) => (
          <label key={day} className="program-scheduling__weekday">
            <input
              type="checkbox"
              checked={value.days[index]}
              onChange={(event) => {
                const days = [...value.days];
                days[index] = event.target.checked;
                patch({ days });
                problems.touch("days");
              }}
            />
            {DAY_NAMES[index]}
          </label>
        ))}
        {daysReason !== null && (
          <p id={daysReasonId} className="field__reason">
            {daysReason}
          </p>
        )}
      </fieldset>
      {time("start", "Starts")}
      {time("end", "Ends")}
      {words !== null && <p className="field__hint" role="status">{words}</p>}
    </>
  );
}

/** A priority's words: "Normal", "Special event", "Always wins". */
export function priorityWords(priority) {
  return PRIORITIES.find(([key]) => key === priority)?.[1] ?? priority;
}

/**
 * Step 3, Review: a check-answers list of every value, each with "Change"; for an edit, the saved
 * Program's window now or next as Central worked it out (`windows`, the snapshot's
 * `program_windows`); then "Program name" and, under Advanced, "Priority" (Normal by default).
 *
 * @param {StepProps & {advanced: {open: boolean, onToggle: () => void},
 *          onChange: (field: string) => void}} props
 */
export function ReviewStep({ value, patch, problems, advanced, onChange, windows = null }) {
  const words = draftWhenWords(value);
  const stored = windows === null ? null : windows.current ?? windows.next ?? null;
  const rows = [
    { label: "Scene", field: "scene", value: value.sceneId === "" ? <NotChosen /> : `Scene ${value.sceneId}` },
    { label: "When", field: "start", value: words === null ? <NotChosen /> : words },
    { label: "Priority", field: "priority", value: priorityWords(value.priority) },
  ];
  return (
    <>
      <TimeZoneNote />
      <CheckAnswers rows={rows} onChange={onChange} />
      {windows !== null && (
        <p className="field__hint" aria-label="Saved windows">
          {stored === null ? "The saved Program has no window in the coming week."
            : `${windows.current ? "Playing now" : "Next window"} (as saved, worked out by Central): ${windowWords(stored)}`}
        </p>
      )}
      <Field id={problems.idFor("name")} label="Program name" reason={problems.reasonFor("name")}>
        {(props) => (
          <input
            {...props}
            type="text"
            maxLength={80}
            value={value.name}
            onChange={(event) => {
              patch({ name: event.target.value });
              problems.touch("name");
            }}
          />
        )}
      </Field>
      <Advanced summary={`Priority: ${priorityWords(value.priority)}`} open={advanced.open} onToggle={advanced.onToggle}>
        <Field id={problems.idFor("priority")} label="Priority" reason={problems.reasonFor("priority")}
          hint="A Schedule with a higher priority plays over one with a lower; the Default Scene plays under every one.">
          {(props) => (
            <select
              {...props}
              value={value.priority}
              onChange={(event) => {
                patch({ priority: event.target.value });
                problems.touch("priority");
              }}
            >
              {PRIORITIES.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
            </select>
          )}
        </Field>
      </Advanced>
    </>
  );
}
