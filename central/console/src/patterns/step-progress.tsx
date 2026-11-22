import type { Severity } from "../design/tokens";
import { cn } from "../ui/cn";
import { SeverityMark } from "../ui/severity";

/** One step of a long action. */
export interface ProgressStepView {
  label: string;
  /** `done` with its time; `current`: what is awaited now; `waiting`: not reached; `failed`: where it stopped. */
  state: "done" | "current" | "waiting" | "failed";
  /** When it happened, already worded ("21:04:07"); null: no time is known. */
  at?: string | null;
  /** A failed step's plain reason ("No answer from the Pi by 21:14"). */
  note?: string;
}

export interface StepProgressProps {
  /** What is in progress, the list's name ("Restarting the photo app on Pi pw-3f2a"). */
  label: string;
  steps: readonly ProgressStepView[];
}

const MARK: Record<ProgressStepView["state"], Severity> = { done: "ok", current: "notice", waiting: "unknown", failed: "alarm" };
const WORDS: Record<ProgressStepView["state"], string> = { done: "Done", current: "Now", waiting: "Not yet", failed: "Did not happen" };

/**
 * StepProgress (design language §4, §5 Feedback timing): a long action across the network as its
 * steps in order ("Reboot requested → started → back online"), each with its state in words and
 * the time it happened. The step awaited now is marked as the current one, and one polite line
 * says it, so a screen reader hears each move once. Nothing is shown as done before its report: a
 * step with no time yet is "Now" or "Not yet". The person may leave and come back.
 */
export function StepProgress({ label, steps }: StepProgressProps) {
  const current = steps.find((step) => step.state === "current" || step.state === "failed") ?? null;
  return (
    <div className="flex min-w-0 flex-col gap-1.5 text-sm text-text">
      <ol aria-label={label} className="m-0 flex list-none flex-col gap-1 p-0 md:flex-row md:flex-wrap md:gap-x-6">
        {steps.map((step) => (
          <li key={step.label} aria-current={step.state === "current" ? "step" : undefined}
            className={cn("flex min-w-0 items-baseline gap-1.5", step.state === "waiting" && "text-muted")}>
            <SeverityMark severity={MARK[step.state]} />
            <span className={cn("wrap-anywhere", step.state === "current" && "font-semibold")}>{step.label}</span>
            <span className="text-xs text-muted">
              {step.state === "done" && step.at != null ? step.at : WORDS[step.state]}
            </span>
          </li>
        ))}
      </ol>
      <p role="status" className="m-0 text-xs text-muted">
        {current === null ? (steps.every((step) => step.state === "done") ? "Finished." : "")
          : current.state === "failed" ? `Stopped at “${current.label}”${current.note !== undefined ? `: ${current.note}` : "."}`
            : `Waiting for “${current.label}”.`}
      </p>
    </div>
  );
}
