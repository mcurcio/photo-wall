import React from "react";

/**
 * One step of a flow as a form (flow design §6, §7; presentational): the step's
 * heading, its fields, and the footer with Back and the step's one forward action.
 *
 * The forward action is the form's submit button, so Enter in a field submits it
 * (Continue, or the flow's final write on Review); `onSubmit` receives no event and
 * nothing is sent by the browser. Back is a plain button. The heading takes focus
 * (`tabIndex=-1`, `data-flow-step-heading`) when a flow moves to the step. Under
 * 850 px the footer is sticky at the bottom of the viewport (index.css).
 *
 * @param {{label: string, heading: string, onSubmit: () => void,
 *          onBack?: (() => void)|null, backLabel?: string, submitLabel?: string,
 *          submitDisabled?: boolean,
 *          submitRef?: React.Ref<HTMLButtonElement>, children: React.ReactNode}} props
 */
export function StepForm({
  label,
  heading,
  onSubmit,
  onBack = null,
  backLabel = "Back",
  submitLabel = "Continue",
  submitDisabled = false,
  submitRef,
  children,
}) {
  return (
    <form
      className="flow__form"
      role="form"
      aria-label={label}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <h3 className="flow__step-title" tabIndex={-1} data-flow-step-heading="">
        {heading}
      </h3>
      <div className="flow__body">{children}</div>
      <div className="flow__footer">
        {onBack !== null && (
          <button type="button" className="flow__back" onClick={onBack}>
            {backLabel}
          </button>
        )}
        <button ref={submitRef} type="submit" className="flow__next" disabled={submitDisabled}>
          {submitLabel}
        </button>
      </div>
    </form>
  );
}
