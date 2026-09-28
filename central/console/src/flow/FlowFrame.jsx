import React from "react";

import { ProblemSummary } from "../Field.jsx";
import { formatRoute } from "../routes.js";
import { NEW_KEY } from "./instance.js";
import { DraftBar, HandOffNotice, InstanceNotice } from "./InstanceNotice.jsx";
import { StepForm } from "./StepForm.jsx";
import { Stepper } from "./Stepper.jsx";

/**
 * One flow's section, framed the same way for every flow (flow design §6, §7;
 * presentational over the flow container's state). Top to bottom:
 *
 *  - THE SAID REGION (the kit's `savedRef`, focused after a write): `said` (what the write
 *    answered, e.g. Show now's outcome), the confirmation's role=status line, then
 *    `next` (e.g. the Scene flow's "Show now" and "Schedule it"). Mark `said` and `next`
 *    with the class `flow__said`, so the region keeps its gap only while it says
 *    something (index.css `.flow__saved`);
 *  - the hand-off notice while the flow runs inline for another (`handOff`, the kit's
 *    `useHandOffTo`), whose "Discard and return" discards the draft (asking first
 *    when it holds changes) and leaves;
 *  - on the section itself: the `DraftBar` (the section's New button, which starts a
 *    new instance, or "Resume draft (Draft)" and "Discard draft"), then `cards`;
 *  - `InstanceNotice` (a route naming an instance the section cannot show);
 *  - on a step: the flow's `title`, the `Stepper` and the step's `StepForm`, holding
 *    `notice` (a note about the draft), the `ProblemSummary` and `children` (the step's
 *    view). The last step's forward action is the flow's write (`onWrite`,
 *    `writeLabel`, `writeDisabled`); every other step's is Continue. While `busy` (the
 *    write in flight) the stepper's steps are text and the form is disabled.
 *
 * `replace` enters the flow in place of the section's history entry (Show now; the
 * kit's `start`/`resume` option). The section's refs are the kit's (`flow.refs`:
 * useFlowInstance REFS).
 *
 * @param {{className?: string,
 *          flow: ReturnType<typeof import("./useFlowInstance.js").useFlowInstance>,
 *          draft: {key: string|null, dirty: boolean},
 *          keys: import("./instance.js").FlowKeys, noun: string, sectionLabel: string,
 *          unavailableReason?: string,
 *          confirm: ReturnType<typeof import("../ConfirmAction.jsx").useConfirm>,
 *          problems: ReturnType<typeof import("../Field.jsx").useProblems>,
 *          writeRef?: React.Ref<HTMLButtonElement>,
 *          said?: React.ReactNode, next?: React.ReactNode,
 *          handOff?: {label: string}|null, newLabel: string, replace?: boolean,
 *          cards?: React.ReactNode, title: string,
 *          steps: ReadonlyArray<import("./steps.js").Step>, formLabel: string,
 *          heading: string, busy?: boolean, onWrite: () => void, writeLabel: string,
 *          writeDisabled?: boolean, problemsLabel: string, notice?: React.ReactNode,
 *          children: React.ReactNode}} props
 */
export function FlowFrame({
  className = "",
  flow,
  draft,
  keys,
  noun,
  sectionLabel,
  unavailableReason = "",
  confirm,
  problems,
  writeRef,
  said = null,
  next = null,
  handOff = null,
  newLabel,
  replace = false,
  cards = null,
  title,
  steps,
  formLabel,
  heading,
  busy = false,
  onWrite,
  writeLabel,
  writeDisabled = false,
  problemsLabel,
  notice = null,
  children,
}) {
  const { place, step, focus } = flow;
  const { savedRef, newRef, summaryRef } = flow.refs;
  const entry = replace ? { replace: true } : undefined;
  const last = step === steps[steps.length - 1].id;

  /** "Discard and return": close the draft (asking first when it holds changes), then leave. */
  const discardAndReturn = (event) => {
    if (draft.dirty) {
      confirm.open(event, flow.discardRequest(flow.leave));
    } else {
      flow.discard(flow.leave);
    }
  };

  return (
    <div className={`flow ${className}`.trim()} ref={focus.rootRef}>
      <div className="flow__saved" ref={savedRef} tabIndex={-1}>
        {said}
        {confirm.confirmation("flow__status-line")}
        {next}
      </div>

      <HandOffNotice handOff={handOff} noun={noun} onDiscard={discardAndReturn} />

      {place === "list" && (
        <>
          <DraftBar
            dirty={draft.dirty}
            draftName={keys.describe(draft.key)}
            newLabel={newLabel}
            newRef={newRef}
            onNew={() => flow.start(NEW_KEY, null, entry)}
            onResume={() => flow.resume(entry)}
            onDiscard={(event) =>
              confirm.open(
                event,
                flow.discardRequest(() => focus.focusWhenShown(() => newRef.current)),
              )
            }
          />
          {cards}
        </>
      )}

      <InstanceNotice
        place={place}
        draftName={keys.describe(draft.key)}
        targetName={flow.routeKey === null ? "" : keys.describe(flow.routeKey)}
        noun={noun}
        unavailableReason={unavailableReason}
        sectionHref={formatRoute({ section: keys.section })}
        sectionLabel={sectionLabel}
        onResume={() => flow.resume({ replace: true })}
        onDiscard={flow.discardForRoute}
      />

      {step !== null && (
        <>
          <h2 className="flow__title">{title}</h2>
          <Stepper
            steps={steps}
            current={step}
            onStep={busy ? undefined : flow.showStep}
            answered={flow.answered}
          />
          <StepForm
            label={formLabel}
            heading={heading}
            onSubmit={last ? onWrite : flow.onContinue}
            onBack={flow.onBack}
            submitLabel={last ? writeLabel : "Continue"}
            submitDisabled={last && writeDisabled}
            busy={busy}
            submitRef={writeRef}
          >
            {notice}
            <ProblemSummary
              ref={summaryRef}
              summary={problems.summary}
              label={problemsLabel}
              onOpen={flow.openField}
            />
            {children}
          </StepForm>
        </>
      )}
    </div>
  );
}
