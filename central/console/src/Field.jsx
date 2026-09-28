import React, { useCallback, useId, useState } from "react";

import { idFromName } from "./authoring.js";

/**
 * Reasons, never silent disables (pass 2 slice 3 §6; prior art: the GOV.UK
 * error-summary pattern). Every authoring form reads its problems
 * (authoring.js) through this one hook and renders them through `Field` and
 * `ProblemSummary`, so the policy is written once:
 *
 *  - a field shows its reason after its first edit (`aria-invalid` plus
 *    `aria-describedby`), or at once when the problem is `immediate`;
 *  - submitting with problems sends nothing, freezes a `role="alert"` summary
 *    at that moment (a poll never rewrites it under the reader), shows every
 *    field's reason and focuses the first field with a problem;
 *  - only an in-flight write disables a button (the caller's concern).
 *
 * @param {import("./authoring.js").Problem[]} problems the live problems
 */
export function useProblems(problems) {
  const prefix = useId();
  const [touched, setTouched] = useState(() => new Set());
  const [submitted, setSubmitted] = useState(false);
  const [summary, setSummary] = useState(
    /** @type {import("./authoring.js").Problem[]|null} */ (null),
  );

  const idFor = useCallback((field) => `${prefix}-${field}`, [prefix]);

  const touch = useCallback((field) => {
    setTouched((previous) => (previous.has(field) ? previous : new Set(previous).add(field)));
  }, []);

  /** The reason to show beside `field` now, or null. */
  const reasonFor = (field) => {
    const problem = problems.find((candidate) => candidate.field === field);
    if (problem === undefined) {
      return null;
    }
    return submitted || problem.immediate || touched.has(field) ? problem.message : null;
  };

  /**
   * On submit: true when there is nothing to fix. Otherwise freeze the
   * summary, show every reason, focus the first field and return false. A form
   * with two actions checks the list for the one submitted.
   */
  const check = (list = problems) => {
    if (list.length === 0) {
      setSummary(null);
      return true;
    }
    setSummary(list);
    setSubmitted(true);
    const first = document.getElementById(idFor(list[0].field));
    const target = first?.matches("input, select, textarea, button")
      ? first
      : first?.querySelector("input, select, textarea, button");
    target?.focus();
    return false;
  };

  /** Back to a pristine form (after a successful save). */
  const reset = useCallback(() => {
    setTouched(new Set());
    setSubmitted(false);
    setSummary(null);
  }, []);

  return { idFor, touch, reasonFor, check, reset, summary };
}

/**
 * One form field: the label above the control, then the hint, then the
 * reason (§12 `.field`). `children` renders the control with the props that
 * tie it to its label, hint and reason.
 *
 * @param {{id: string, label: React.ReactNode, hint?: React.ReactNode,
 *          reason?: string|null, className?: string,
 *          children: (props: object) => React.ReactNode}} props
 */
export function Field({ id, label, hint = null, reason = null, className = "", children }) {
  const hintId = hint !== null ? `${id}-hint` : null;
  const reasonId = reason !== null ? `${id}-reason` : null;
  const describedBy = [hintId, reasonId].filter((part) => part !== null).join(" ");
  return (
    <div className={`field ${className}`.trim()}>
      <label className="field__label" htmlFor={id}>
        {label}
      </label>
      {children({
        id,
        "aria-describedby": describedBy === "" ? undefined : describedBy,
        "aria-invalid": reason !== null ? true : undefined,
      })}
      {hint !== null && (
        <p id={hintId} className="field__hint">
          {hint}
        </p>
      )}
      {reason !== null && (
        <p id={reasonId} className="field__reason">
          {reason}
        </p>
      )}
    </div>
  );
}

/**
 * The name and the id it saves under (§5): "Saved as `family-evening` ·
 * Change". Change — or a name with no usable id — reveals the Id field, which
 * then decides the id. Scenes and Programs both name themselves through here.
 *
 * @param {{kind: string, name: string, idOverride: string|null,
 *          onName: (name: string) => void,
 *          onIdOverride: (id: string|null) => void,
 *          problems: ReturnType<typeof useProblems>}} props
 */
export function IdentityFields({ kind, name, idOverride, onName, onIdOverride, problems }) {
  const derived = idFromName(name);
  const idShown = idOverride !== null || (name.trim() !== "" && derived === null);
  const nameField = problems.idFor("name");
  return (
    <>
      <Field
        id={nameField}
        label={`${kind} name`}
        reason={problems.reasonFor("name")}
        hint={
          !idShown && derived !== null ? (
            <>
              {"Saved as "}
              <code>{derived}</code>
              {" · "}
              <button
                type="button"
                className="field__inline-action"
                onClick={() => {
                  onIdOverride(derived);
                  problems.touch("id");
                }}
              >
                Change
              </button>
            </>
          ) : null
        }
      >
        {(props) => (
          <input
            {...props}
            type="text"
            value={name}
            onChange={(event) => {
              onName(event.target.value);
              problems.touch("name");
            }}
          />
        )}
      </Field>
      {idShown && (
        <Field id={problems.idFor("id")} label="Id" reason={problems.reasonFor("id")}>
          {(props) => (
            <input
              {...props}
              type="text"
              value={idOverride ?? ""}
              autoCapitalize="off"
              autoCorrect="off"
              spellCheck={false}
              onChange={(event) => {
                onIdOverride(event.target.value);
                problems.touch("id");
              }}
            />
          )}
        </Field>
      )}
    </>
  );
}

/**
 * The submit summary: the problems as they were when the operator submitted.
 *
 * @param {{summary: import("./authoring.js").Problem[]|null, label: string}} props
 */
export function ProblemSummary({ summary, label }) {
  if (summary === null) {
    return null;
  }
  return (
    <div className="problems" role="alert" aria-label={label}>
      <p className="problems__title">Nothing was sent. Fix these first:</p>
      <ul className="problems__list">
        {summary.map((problem) => (
          <li key={`${problem.field}:${problem.message}`}>{problem.message}</li>
        ))}
      </ul>
    </div>
  );
}
