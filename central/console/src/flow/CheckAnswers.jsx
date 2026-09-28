import React from "react";

/**
 * Review's check-answers list (flow design §7 "Check answers", after the GOV.UK
 * pattern; presentational): each answer's label and value, with a "Change" button
 * (named "Change <label>") that calls `onChange(field)`, which a flow routes to the
 * field's step (`useFlowInstance().openField`). Every value is listed, the advanced
 * ones included.
 *
 * @param {{rows: ReadonlyArray<{label: string, field: string, value: React.ReactNode}>,
 *          onChange: (field: string) => void}} props
 */
export function CheckAnswers({ rows, onChange }) {
  return (
    <dl className="review" aria-label="Your answers">
      {rows.map((row) => (
        <div key={row.label} className="review__row">
          <dt className="review__key">{row.label}</dt>
          <dd className="review__value">{row.value}</dd>
          <dd className="review__change">
            <button
              type="button"
              className="review__change-button"
              aria-label={`Change ${row.label}`}
              onClick={() => onChange(row.field)}
            >
              Change
            </button>
          </dd>
        </div>
      ))}
    </dl>
  );
}
