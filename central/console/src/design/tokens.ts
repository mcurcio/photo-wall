/**
 * The design language's vocabulary (console design system): the one severity scale and
 * the six truth kinds. Each value names a colour token in tokens.css (`--color-<severity>`,
 * `--color-truth-<kind>`); the wording of each stays with its domain model.
 */

/** The ONE severity scale; it replaces the four per-model typedefs as models move. */
export const SEVERITIES = ["ok", "todo", "notice", "alarm", "unknown"] as const;

/**
 * - `ok`
 * - `todo`: work to finish, not a fault
 * - `notice`
 * - `alarm`: lost something it had
 * - `unknown`: not read, not served
 */
export type Severity = (typeof SEVERITIES)[number];

/** A fact's truth kind: the six of facts.js `KINDS` (a test keeps the two equal). */
export const TRUTH_KINDS = ["set", "reported", "claimed", "derived", "planned", "unknown"] as const;

export type TruthKind = (typeof TRUTH_KINDS)[number];

/**
 * The severities worst first: the one order a "worst first" list sorts by. `unknown` (not
 * read) ranks below a judged notice and above work to finish and ok.
 */
export const WORST_FIRST: readonly Severity[] = ["alarm", "notice", "unknown", "todo", "ok"];
