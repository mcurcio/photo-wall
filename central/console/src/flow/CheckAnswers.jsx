import React from "react";

/**
 * A flow's Review as a check-answers list (flow design §7 "Check answers";
 * presentational): each answer by its label, with a "Change" button (named "Change
 * <label>") that routes to the field asking it (`onChange(field)`, the kit's
 * `openField`). A row without `field` is a fact of the answers, with no Change.
 *
 * @param {{rows: ReadonlyArray<{label: string, value: React.ReactNode, field?: string}>,
 *          onChange: (field: string) => void, label?: string}} props
 */
export function CheckAnswers({ rows, onChange, label = "Your answers" }) {
  return (
    <dl className="review" aria-label={label}>
      {rows.map((row) => (
        <div key={row.label} className="review__row">
          <dt className="review__key">{row.label}</dt>
          <dd className="review__value">{row.value}</dd>
          <dd className="review__change">
            {row.field !== undefined && (
              <button
                type="button"
                className="review__change-button"
                aria-label={`Change ${row.label}`}
                onClick={() => onChange(row.field)}
              >
                Change
              </button>
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** How a check-answers value not yet given reads. */
export function NotChosen() {
  return <span className="review__missing">Not chosen</span>;
}
