import React from "react";

import { Field } from "./Field.jsx";
import { Advanced } from "./flow/Advanced.jsx";
import { CheckAnswers, NotChosen } from "./flow/CheckAnswers.jsx";
import { PreviewPanel } from "./PreviewPanel.jsx";
import {
  ANOTHER_CONNECTION,
  FAVOURITES_CHOICES,
  MEDIA_TYPE_CHOICES,
  selectionWords,
  SOURCE_LABELS,
  sourceAnswers,
} from "./sourceFlowModel.js";
import { TagCombobox } from "./TagCombobox.jsx";
import { zoneName } from "./timeWords.js";

/**
 * The Source flow's step views (console DDD §37): views over the draft that SourceFlow.jsx
 * owns. They hold no draft state. Labels and accessible names are sourceFlowModel.js
 * `SOURCE_LABELS`.
 *
 * Every view takes `{value, patch, problems}`: the draft value, the draft's patch and
 * the flow's `useProblems`. The Tags, Narrow and Review steps also take `panel`: the
 * preview panel's props (PreviewPanel.jsx), the same preview on all three.
 *
 * @typedef {import("./sourceFlowModel.js").SourceDraft} SourceDraft
 * @typedef {{value: SourceDraft, patch: (partial: Partial<SourceDraft>) => void,
 *            problems: ReturnType<typeof import("./Field.jsx").useProblems>}} StepProps
 */

/** A select of `[value, words]` choices for problem field `field`, labelled as the form labels it. */
function ChoiceField({ field, choices, value, onChange, problems }) {
  return (
    <Field id={problems.idFor(field)} label={SOURCE_LABELS[field]} reason={problems.reasonFor(field)}>
      {(props) => (
        <select
          {...props}
          value={value}
          onChange={(event) => {
            onChange(event.target.value);
            problems.touch(field);
          }}
        >
          {choices.map(([choice, words]) => (
            <option key={choice} value={choice}>
              {words}
            </option>
          ))}
        </select>
      )}
    </Field>
  );
}

/**
 * A text or date input for problem field `field`, labelled as the form labels it (or
 * `label`).
 */
function InputField({
  field,
  label = SOURCE_LABELS[field],
  type = "text",
  hint = null,
  value,
  onChange,
  problems,
}) {
  const text = type === "text" ? { autoCapitalize: "off", autoCorrect: "off", spellCheck: false } : {};
  return (
    <Field
      id={problems.idFor(field)}
      label={label}
      hint={hint}
      reason={problems.reasonFor(field)}
    >
      {(props) => (
        <input
          {...props}
          {...text}
          type={type}
          value={value}
          onChange={(event) => {
            onChange(event.target.value);
            problems.touch(field);
          }}
        />
      )}
    </Field>
  );
}

/**
 * Step 1, Which library connection? (skipped when exactly one is known): the connection
 * rule's chooser for several, the setup prerequisite when the worker reports none, or a
 * typed name, marked uncertain, until the worker reports its list.
 *
 * @param {StepProps & {rule: ReturnType<typeof import("./sourceFlowModel.js").connectionRule>}} props
 */
export function LibraryStep({ value, patch, problems, rule }) {
  return <ConnectionField value={value} patch={patch} problems={problems} rule={rule} />;
}

/**
 * Step 2, Choose tags: the tag picker over the connection's tags, beside what the chosen
 * tags select. Zero tags is allowed: everything in the library.
 *
 * @param {StepProps & {panel: object, tags: {connection: string,
 *          paths: Readonly<Record<string, string|null>>, onLearn: Function}|null}} props
 */
export function TagsStep({ value, patch, problems, panel, tags }) {
  if (tags === null) {
    return <PreviewPanel {...panel} />; // it says why there are no tags or preview
  }
  return (
    <div className="source-flow__split">
      <div className="source-flow__criteria">
        <TagCombobox
          id={problems.idFor("tags")}
          connection={tags.connection}
          value={value.tags ?? []}
          paths={tags.paths}
          onChange={(next) => patch({ tags: next })}
          onLearn={tags.onLearn}
        />
      </div>
      <PreviewPanel {...panel} />
    </div>
  );
}

/**
 * Step 3, Narrow it down (optional): "Media type" (default photos and videos),
 * "Favourites" (default any), "Dated from" and "Dated until" (empty), beside the same
 * preview, following the criteria.
 *
 * @param {StepProps & {panel: object}} props
 */
export function NarrowStep({ value, patch, problems, panel }) {
  return (
    <div className="source-flow__split">
      <div className="source-flow__criteria">
        <ChoiceField
          field="type"
          choices={MEDIA_TYPE_CHOICES}
          value={value.mediaType}
          onChange={(mediaType) => patch({ mediaType })}
          problems={problems}
        />
        <ChoiceField
          field="favorites"
          choices={FAVOURITES_CHOICES}
          value={value.favorites}
          onChange={(favorites) => patch({ favorites })}
          problems={problems}
        />
        <InputField
          field="from"
          type="date"
          hint={`From the start of this day in this browser's time zone (${zoneName()}).`}
          value={value.capturedFrom}
          onChange={(capturedFrom) => {
            patch({ capturedFrom });
            problems.touch("until"); // the window's problem is said beside "Dated until"
          }}
          problems={problems}
        />
        <InputField
          field="until"
          type="date"
          hint="Up to the start of this day."
          value={value.capturedUntil}
          onChange={(capturedUntil) => patch({ capturedUntil })}
          problems={problems}
        />
      </div>
      <PreviewPanel {...panel} />
    </div>
  );
}

/**
 * The connection rule's chooser offers each reported connection. A saved name
 * removed from the worker appears disabled until replaced. "Another connection…"
 * is available only before the worker reports its list; then the operator must
 * type a name that is already configured there.
 *
 * @param {StepProps & {rule: ReturnType<typeof import("./sourceFlowModel.js").connectionRule>}} props
 */
export function ConnectionChooser({ value, patch, problems, rule }) {
  // A worker report can arrive while a legacy manual-entry draft is open.
  // Once the list is known, keep its saved value visible as unavailable and
  // make the chooser usable again instead of selecting a removed "Another" option.
  const typed = value.newConnection && !rule.reported;
  const served = [...new Set([...rule.values, rule.selectedUnavailable ? value.connectionRef : ""])]
    .filter((ref) => ref !== "")
    .sort();
  const id = problems.idFor("connection");
  return (
    <>
      <Field
        id={typed ? `${id}-choice` : id}
        label={SOURCE_LABELS.connection}
        reason={typed ? null : problems.reasonFor("connection")}
      >
        {(props) => (
          <select
            {...props}
            value={typed ? ANOTHER_CONNECTION.value : value.connectionRef}
            onChange={(event) => {
              const choice = event.target.value;
              if (choice === ANOTHER_CONNECTION.value) {
                patch({ connectionRef: "", newConnection: true });
              } else {
                patch({ connectionRef: choice, newConnection: false });
                problems.touch("connection");
              }
            }}
          >
            <option value="">Choose a configured connection</option>
            {served.map((ref) => (
              <option key={ref} value={ref} disabled={rule.reported && !rule.values.includes(ref)}>
                {rule.reported && !rule.values.includes(ref) ? `${ref} (no longer configured)` : ref}
              </option>
            ))}
            {!rule.reported && <option value={ANOTHER_CONNECTION.value}>{ANOTHER_CONNECTION.words}</option>}
          </select>
        )}
      </Field>
      {typed && (
        <InputField
          field="connection"
          label={ANOTHER_CONNECTION.label}
          hint="The media worker has not reported its configured connections yet. Enter a name only if it is already configured there; this form does not set the library's address or key."
          value={value.connectionRef}
          onChange={(connectionRef) => patch({ connectionRef })}
          problems={problems}
        />
      )}
    </>
  );
}

/**
 * The connection as the connection rule asks it (sourceFlowModel.js `connectionRule`): an
 * explicit setup prerequisite when the worker reports none; a chooser for several or a
 * removed saved name; the one known connection (under the Name step's Advanced); or an
 * uncertain typed name until the worker reports its list.
 *
 * @param {StepProps & {rule: ReturnType<typeof import("./sourceFlowModel.js").connectionRule>}} props
 */
function ConnectionField({ value, patch, problems, rule }) {
  if (rule.shown === "blocked") {
    return (
      <>
        <Field id={problems.idFor("connection")} label={SOURCE_LABELS.connection} reason={problems.reasonFor("connection")}>
          {(props) => (
            <select {...props} value="" disabled>
              <option value="">No connections configured</option>
            </select>
          )}
        </Field>
        <p role="status">Add a connection to the media worker's private configuration and restart the worker. Then return here to create the Source.</p>
      </>
    );
  }
  if (rule.shown === "chooser") {
    return <ConnectionChooser value={value} patch={patch} problems={problems} rule={rule} />;
  }
  if (rule.reported) {
    return (
      <Field id={problems.idFor("connection")} label={SOURCE_LABELS.connection} reason={problems.reasonFor("connection")}>
        {(props) => (
          <select
            {...props}
            value={value.connectionRef}
            onChange={(event) => {
              patch({ connectionRef: event.target.value });
              problems.touch("connection");
            }}
          >
            {value.connectionRef === "" && <option value="">Choose the configured connection</option>}
            {rule.values.map((ref) => <option key={ref} value={ref}>{ref}</option>)}
          </select>
        )}
      </Field>
    );
  }
  return (
    <InputField
      field="connection"
      hint={rule.shown === "field"
        ? "The media worker has not reported its configured connections yet. You can enter a name, but Central cannot verify it. This form does not set the library's address or key."
        : "Central has not received the worker's connection list yet. This saved name may need checking in the worker configuration."}
      value={value.connectionRef}
      onChange={(connectionRef) => patch({ connectionRef })}
      problems={problems}
    />
  );
}

/**
 * Step 4, Name this Source: a plain Source name (required); with exactly one known
 * connection, that connection under Advanced (the connection step was skipped).
 *
 * @param {StepProps & {rule: ReturnType<typeof import("./sourceFlowModel.js").connectionRule>,
 *          advanced: {open: boolean, onToggle: () => void}}} props
 */
export function NameStep({ value, patch, problems, rule, advanced, editing = false }) {
  return (
    <>
      <InputField
        field="ref"
        hint={editing
          ? "Renaming also updates the Scenes that use this Source for future Runs."
          : "For example, all-photos."}
        value={value.sourceName}
        onChange={(sourceName) => patch({ sourceName })}
        problems={problems}
      />
      {rule.shown === "advanced" && (
        <Advanced
          summary={`Connection: ${value.connectionRef.trim() || "none"}`}
          open={advanced.open}
          onToggle={advanced.onToggle}
        >
          <ConnectionField value={value} patch={patch} problems={problems} rule={rule} />
        </Advanced>
      )}
    </>
  );
}

/**
 * Step 5, Check your Source: what it selects, every answer with "Change" (`onChange(field)`
 * routes to the field's step, and opens Advanced for the connection when it sits there),
 * and the preview.
 *
 * @param {{value: SourceDraft, onChange: (field: string) => void, rule: object,
 *          paths: Readonly<Record<string, string|null>>, spec: object, panel: object}} props
 */
export function SourceReview({ value, onChange, rule, paths, spec, panel }) {
  const rows = sourceAnswers(value, rule, paths).map((answer) => ({
    ...answer,
    value: answer.value ?? <NotChosen />,
  }));
  return (
    <>
      <p className="source-flow__selects">{selectionWords(spec, paths)}</p>
      <CheckAnswers rows={rows} onChange={onChange} />
      <PreviewPanel {...panel} />
    </>
  );
}
