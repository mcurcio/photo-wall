import { ProblemCard } from "../patterns/problem-card";
import { CHECK_FAILED, failedWords, PHOTOS_KEEP_PLAYING } from "../sourceWords.js";
import type { CheckState } from "./source-pool-view";

export interface SourceCheckProblemProps {
  /** The Source's name, leading the problem. */
  name: string;
  /** Its served check; anything but a failed one shows nothing. */
  check: CheckState | null | undefined;
}

/**
 * SourceCheckProblem: a Source whose last check failed, as the error template (ProblemCard) where
 * a Source is chosen or shown (the Scene editor, Show now), so a failure is a block and never a
 * line of body text. Its words name who failed (sourceWords.js `ORIGINS`); the severity stays
 * `notice`, since photos keep playing. A connection problem's one fix is on the Photo library page.
 */
export function SourceCheckProblem({ name, check }: SourceCheckProblemProps) {
  if (check?.state !== "failed") return null;
  return (
    <ProblemCard
      scope="setup"
      subject={name}
      variant="inline"
      verdict={{ severity: "notice", label: CHECK_FAILED, receipt: null }}
      what={failedWords(check)}
      doing={PHOTOS_KEEP_PLAYING}
      action={check.problem === "connection" ? { label: "Open Photo library", href: "#/photo-library" } : undefined}
    />
  );
}
