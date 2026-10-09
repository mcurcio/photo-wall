import { Alert, AlertLine } from "../ui/alert";

/** A failing Source's problem in its model's words (mediaHealth.js `sourceProblem`). */
export interface SourceProblemWords {
  title: string;
  lines: readonly string[];
}

export interface SourceProblemProps {
  problem: SourceProblemWords;
  /** The Source's name, leading the title where several Sources are listed together. */
  name?: string;
}

/**
 * SourceProblem: a failing Source as an error alert: what failed, why, the one fix and when
 * it last refreshed successfully. The words are the model's; this only lays them out.
 */
export function SourceProblem({ problem, name }: SourceProblemProps) {
  return (
    <Alert severity="alarm" title={name ? `${name}: ${problem.title}` : problem.title}>
      {problem.lines.map((line) => (
        <AlertLine key={line}>{line}</AlertLine>
      ))}
    </Alert>
  );
}
