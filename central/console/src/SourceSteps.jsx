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
 * The connection rule's chooser (several connections): "Connection name" offers each
 * served connection and, last, "Another connection…", which shows "New connection name"
 * for one no Source uses yet. The connection's problem is then said, and focused,
 * beside the typed field.
 *
 * @param {StepProps & {rule: ReturnType<typeof import("./sourceFlowModel.js").connectionRule>}} props
 */
function ConnectionChooser({ value, patch, problems, rule }) {
  const typed = value.newConnection;
  const served = [...new Set([...rule.values, typed ? "" : value.connectionRef])]
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
            <option value="">Choose a connection</option>
            {served.map((ref) => (
              <option key={ref} value={ref}>
                {ref}
              </option>
            ))}
            <option value={ANOTHER_CONNECTION.value}>{ANOTHER_CONNECTION.words}</option>
          </select>
        )}
      </Field>
      {typed && (
        <InputField
          field="connection"
          label={ANOTHER_CONNECTION.label}
          hint="A library connection no photo source uses yet."
          value={value.connectionRef}
          onChange={(connectionRef) => patch({ connectionRef })}
          problems={problems}
        />
      )}
    </>
  );
}

/**
 * Step 2, Name: "Source name and revision" (required), then "Connection name" as the
 * connection rule says (sourceFlowModel.js `connectionRule`): a visible text field
 * while no Source names one; under Advanced, prefilled, when every Source names the
 * same one; a visible chooser of them, and of another one, when they name several.
 *
 * @param {StepProps & {rule: ReturnType<typeof import("./sourceFlowModel.js").connectionRule>,
 *          advanced: {open: boolean, onToggle: () => void}}} props
 */
export function NameStep({ value, patch, problems, rule, advanced }) {
  const connection =
    rule.shown === "chooser" ? (
      <ConnectionChooser value={value} patch={patch} problems={problems} rule={rule} />
    ) : (
      <InputField
        field="connection"
        hint={rule.shown === "field" ? "The library connection this Source reads from." : null}
        value={value.connectionRef}
        onChange={(connectionRef) => patch({ connectionRef })}
        problems={problems}
      />
    );
  return (
    <>
      <InputField
        field="ref"
        hint="Like holiday:1."
        value={value.sourceRef}
        onChange={(sourceRef) => patch({ sourceRef })}
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
