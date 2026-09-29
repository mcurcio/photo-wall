import React from "react";

import { Field } from "./Field.jsx";
import { Advanced } from "./flow/Advanced.jsx";
import { CheckAnswers, NotChosen } from "./flow/CheckAnswers.jsx";
import {
  ANOTHER_CONNECTION,
  FAVOURITES_CHOICES,
  MEDIA_TYPE_CHOICES,
  SOURCE_LABELS,
  sourceAnswers,
} from "./sourceFlowModel.js";

/**
 * The Source flow's step views (flow design §7 J5): views over the draft that
 * SourceFlow.jsx owns. They hold no draft state. Labels and accessible names are the
 * single form's (rule 3; sourceFlowModel.js `SOURCE_LABELS`).
 *
 * Every view takes `{value, patch, problems}`: the draft value, the draft's patch and
 * the flow's `useProblems`.
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
 * PASS B'S SLOT (flow design §2 req 3, §7 J5; library design §8). An empty container
 * that pass B fills: `criteria` with its tag picker, `preview` with the previews of
 * what the Source selects. It renders nothing in this pass: no role, no text, no
 * space (index.css hides it while empty), so nothing is announced or promised.
 *
 * @param {{name: "criteria"|"preview"}} props
 */
export function LibrarySlot({ name }) {
  return <div className={`source-flow__slot source-flow__slot--${name}`} data-slot={name} />;
}

/**
 * Step 1, What to include: "Media type" (default images and video), "Favourites"
 * (default any), "Taken from" and "Taken until" (empty), then pass B's criteria and
 * preview slots.
 *
 * @param {StepProps} props
 */
export function IncludeStep({ value, patch, problems }) {
  return (
    <>
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
          hint="From the start of this day."
          value={value.capturedFrom}
          onChange={(capturedFrom) => {
            patch({ capturedFrom });
            problems.touch("until"); // the window's problem is said beside "Taken until"
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
        <LibrarySlot name="criteria" />
      </div>
      <LibrarySlot name="preview" />
    </>
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
function ConnectionChooser({ value, patch, problems, rule }) {
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
          hint="The media worker has not reported its configured connections yet. Enter a name only if it is already configured there; this form does not set the Immich URL or API key."
          value={value.connectionRef}
          onChange={(connectionRef) => patch({ connectionRef })}
          problems={problems}
        />
      )}
    </>
  );
}

/**
 * Step 2, Name: a plain Source name (required), then "Connection name" as the
 * connection rule says (sourceFlowModel.js `connectionRule`): an explicit setup
 * prerequisite when the worker reports none; one known connection under Advanced;
 * a chooser for several or a removed saved name; or an uncertain manual fallback
 * until the worker reports its list.
 *
 * @param {StepProps & {rule: ReturnType<typeof import("./sourceFlowModel.js").connectionRule>,
 *          advanced: {open: boolean, onToggle: () => void}}} props
 */
export function NameStep({ value, patch, problems, rule, advanced, editing = false }) {
  const connection = rule.shown === "blocked" ? (
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
  ) : rule.shown === "chooser" ? (
      <ConnectionChooser value={value} patch={patch} problems={problems} rule={rule} />
    ) : rule.reported ? (
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
    ) : (
      <InputField
        field="connection"
        hint={rule.shown === "field"
          ? "The media worker has not reported its configured connections yet. You can enter a name, but Central cannot verify it. This form does not set the Immich URL or API key."
          : "Central has not received the worker's connection list yet. This saved name may need checking in the worker configuration."}
        value={value.connectionRef}
        onChange={(connectionRef) => patch({ connectionRef })}
        problems={problems}
      />
    );
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
      {rule.shown === "advanced" ? (
        <Advanced
          summary={`Connection: ${value.connectionRef.trim() || "none"}`}
          open={advanced.open}
          onToggle={advanced.onToggle}
        >
          {connection}
        </Advanced>
      ) : (
        connection
      )}
    </>
  );
}

/**
 * Step 3, Review: every answer, each with "Change" (`onChange(field)` routes to the
 * field's step, and opens Advanced for the connection when it sits there).
 *
 * @param {{value: SourceDraft, onChange: (field: string) => void}} props
 */
export function SourceReview({ value, onChange }) {
  const rows = sourceAnswers(value).map((answer) => ({
    ...answer,
    value: answer.value ?? <NotChosen />,
  }));
  return <CheckAnswers rows={rows} onChange={onChange} />;
}
