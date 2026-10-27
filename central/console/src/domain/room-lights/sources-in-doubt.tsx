import type * as React from "react";

import type { CentralErrorsView } from "../../generated/contract";
import { Note } from "../../patterns/fact-row";
import { ProblemCard } from "../../patterns/problem-card";
import { Stack } from "../../ui/stack";
import { causeAndFix, doubtVerdict, KEEPING_WORDS, referenceWords, subjectWords } from "./model";

/** What Needs attention read of Central's errors: not read yet (null view, not failed), read, or a
 * read that failed (the last view kept, if any). */
export interface SourcesRead {
  view: CentralErrorsView | null;
  failed: boolean;
}

export interface SourcesInDoubtProps {
  read: SourcesRead;
  /** Each room light's name by its id (none read: the ids are said). */
  names: Readonly<Record<string, string>>;
  /** An instant in words, on the house clock. */
  since: (epochSeconds: number) => string;
  /** The link to a Frame's page that Needs attention renders. */
  frameLink: (frameId: string) => React.ReactNode;
  /** House › Lights, where each light is. */
  lightsHref: string;
}

/** Whether Needs attention may say "Nothing needs attention" for Central's errors: only once a read
 * answered with none (a failed or pending read is never "nothing"). */
export function noneInDoubt(read: SourcesRead): boolean {
  return !read.failed && read.view !== null && read.view.in_doubt.length === 0;
}

/** Whether there is anything to draw: a row in doubt, or a read that failed. */
export function sourcesToShow(read: SourcesRead): boolean {
  return read.failed || (read.view?.in_doubt.length ?? 0) > 0;
}

/**
 * Needs attention's source rows (Central's errors): one `ProblemCard` per reference Central is in
 * doubt of now — the room lights it reports, by name, with the doubt and since when; why, and what
 * to do, in Central's words (`what_to_do`, its source's advice); that the Frames using it draw with
 * its last word meanwhile; a link to House › Lights; the Frames whose lighting names those lights,
 * each a link to its page; where the word comes from under Details. A read that failed says so and
 * keeps the rows last read (never "nothing in doubt"); before the first answer it draws nothing. The
 * host draws the section's heading when `sourcesToShow`.
 */
export function SourcesInDoubt({ read, names, since, frameLink, lightsHref }: SourcesInDoubtProps) {
  const rows = read.view?.in_doubt ?? [];
  if (!sourcesToShow(read)) return null;
  return (
    <Stack label="Room lights in doubt">
      {read.failed && (
        <ProblemCard scope="central" variant="inline" verdict={{ severity: "unknown", label: "Can't tell", receipt: null }}
          what={read.view === null
            ? "Central did not answer with the room lights it is in doubt of."
            : "Central did not answer the last read: these are the room lights in doubt as last read."}
          doing="Photo Wall keeps trying." />
      )}
      {rows.map((row) => (
        <div key={`${row.source}:${row.address}`}>
          <ProblemCard scope="live" subject={subjectWords(row, names)} since={since(row.doubt.since)}
            verdict={doubtVerdict(row)} what={causeAndFix(row)} doing={KEEPING_WORDS}
            action={{ label: "Open House › Lights", href: lightsHref }}
            details={<Note>{`Reported by ${referenceWords(row)}`}</Note>} />
          {row.frames.length > 0 && (
            <Note>
              {"Used by "}
              {row.frames.map((frameId, index) => (
                <span key={frameId}>{index > 0 && ", "}{frameLink(frameId)}</span>
              ))}
            </Note>
          )}
        </div>
      ))}
    </Stack>
  );
}
