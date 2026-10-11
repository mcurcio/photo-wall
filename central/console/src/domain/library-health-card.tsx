import type { Verdict } from "../patterns/health-badge";
import { ProblemCard, type ProblemAction } from "../patterns/problem-card";
import { Note } from "../patterns/fact-row";
import { countWords, sinceTime } from "../timeWords.js";

/*
 * A photo library connection as Central serves it (central/photo_library/views.py
 * `LibraryConnectionView`, the one home of the shape) and its health in words: the verdict, each
 * problem's sentence and its one fix (design-language §8 row A3; run ledger K1). Every problem
 * says "Photos keep playing" (C7): a connection that needs a look never empties the wall.
 */

export interface ImmichUser { user_ref: string; name: string; email: string }
export interface LibraryCounts { photos: number; videos: number; albums: number; people: number }

export type LibraryProblem =
  | { kind: "key_rejected" }
  | { kind: "missing_permissions"; missing: string[] }
  | { kind: "immich_too_old"; found: string; oldest: string }
  | { kind: "unreachable" }
  | { kind: "unexpected_answer"; detail: string }
  | { kind: "other_user"; expected: string; found: string };

export type LibraryNote = { kind: "untested_version"; found: string } | { kind: "no_full_size_copies" };

export type LibraryHealth =
  | { state: "not_checked" }
  | { state: "ok"; checked_at: number; notes: LibraryNote[] }
  | { state: "needs_a_look"; problem: LibraryProblem; since: number; checked_at: number };

export interface LibraryConnectionView {
  connection_ref: string;
  name: string;
  /** Null on a connection waiting since the cut-over: its address and key are pasted once. */
  url: string | null;
  key_hint: string | null;
  immich_user: ImmichUser | null;
  health: LibraryHealth;
  counts: LibraryCounts | null;
  required_permissions: string[];
}

/** The one thing to do about a problem: paste a new key, or ask Photo Wall to check again. */
export type LibraryFix = "paste_key" | "check_again";

/** "41,203 photos · 2,110 videos · 212 albums · 96 people": Immich's own counts. */
export function libraryCountsWords(counts: LibraryCounts): string {
  return [
    countWords(counts.photos, "photo"),
    countWords(counts.videos, "video"),
    countWords(counts.albums, "album"),
    countWords(counts.people, "person", "people"),
  ].join(" · ");
}

/** A cut-over connection (C12): it has a name and Sources use it, but no address or key yet. */
export function waitingForKey(connection: LibraryConnectionView): boolean {
  return connection.url === null;
}

/** The connection's health as a verdict, on the one severity scale. */
export function libraryVerdict(connection: LibraryConnectionView): Verdict {
  if (waitingForKey(connection)) return { severity: "todo", label: "Paste the address and key", receipt: null };
  const { health } = connection;
  if (health.state === "not_checked") return { severity: "notice", label: "Checking", receipt: null };
  if (health.state === "ok") return { severity: "ok", label: "Connected", receipt: `checked ${sinceTime(health.checked_at)}` };
  return { severity: "notice", label: "Needs a look", receipt: `checked ${sinceTime(health.checked_at)}` };
}

interface ProblemWords {
  what: string;
  fix: LibraryFix;
  label: string;
  details?: string;
}

/** Each problem's sentence and its one fix (run ledger K1). */
export function problemWords(connection: LibraryConnectionView, problem: LibraryProblem): ProblemWords {
  switch (problem.kind) {
    case "key_rejected":
      return { what: "Immich rejected the API key.", fix: "paste_key", label: "Paste a new key" };
    case "missing_permissions":
      return {
        what: `The key cannot do everything Photo Wall needs. Make a key with these permissions: ${problem.missing.join(", ")}.`,
        fix: "paste_key",
        label: "Paste a new key",
      };
    case "immich_too_old":
      return {
        what: `Immich ${problem.found} is older than ${problem.oldest}. Update Immich, then check again.`,
        fix: "check_again",
        label: "Check again",
      };
    case "unreachable":
      return {
        what: `Photo Wall cannot reach Immich at ${connection.url ?? "its address"}.`,
        fix: "check_again",
        label: "Check again",
      };
    case "unexpected_answer":
      return {
        what: "Immich answered in a way Photo Wall does not understand.",
        fix: "check_again",
        label: "Check again",
        details: problem.detail,
      };
    case "other_user":
      return {
        what: `This key belongs to ${problem.found}; the connection belongs to ${problem.expected}.`,
        fix: "paste_key",
        label: `Paste a key for ${problem.expected}`,
      };
  }
}

/** The notes of a passing check: shown, nothing stops. */
export function noteWords(note: LibraryNote): string {
  return note.kind === "untested_version"
    ? `Immich ${note.found} is newer than Photo Wall has been tested with; it keeps working.`
    : "RAW photos without a full-size copy in Immich play at Immich's preview size. Turn on full-size copies in Immich to show them in full.";
}

export interface LibraryHealthCardProps {
  connection: LibraryConnectionView;
  /** The fix, on the connection's own page; without it the fix is a link to that page. */
  onFix?: (fix: LibraryFix) => void;
  /** Where the connection is set (Settings › Photo library), for a card shown elsewhere. */
  href?: string;
}

/** How a connection is named where it is not on its own page: "Archive (Immich)". */
export function connectionSubject(connection: LibraryConnectionView): string {
  return `${connection.name} (Immich)`;
}

/**
 * LibraryHealthCard: a connection that needs a look, as the error template (ProblemCard) with its
 * one fix, for Needs attention and Settings › Photo library: "Archive (Immich): Needs a look since
 * Sun 03:39. Immich rejected the API key. Photos keep playing." The fix is the same button on both
 * pages; on Needs attention it opens Settings › Photo library, where the fix is made. Nothing when
 * the connection needs no look.
 */
export function LibraryHealthCard({ connection, onFix, href = "#/photo-library" }: LibraryHealthCardProps) {
  const { health } = connection;
  if (health.state !== "needs_a_look") return null;
  const words = problemWords(connection, health.problem);
  const action: ProblemAction = onFix
    ? { label: words.label, onAction: () => onFix(words.fix) }
    : { label: words.label, onAction: () => { window.location.hash = href; } };
  return (
    <ProblemCard
      scope="live"
      subject={connectionSubject(connection)}
      since={sinceTime(health.since)}
      verdict={libraryVerdict(connection)}
      what={words.what}
      doing="Photos keep playing."
      action={action}
      details={words.details === undefined ? undefined : <Note>{`Immich's answer: ${words.details}`}</Note>}
    />
  );
}
