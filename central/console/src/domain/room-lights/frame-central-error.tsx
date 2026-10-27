import { useCallback } from "react";

import type { FrameImpediment, FrameImpedimentView } from "../../generated/contract";
import { FactRow, Note } from "../../patterns/fact-row";
import { OwnerLinks } from "../../patterns/link-to-owner";
import { ProblemCard } from "../../patterns/problem-card";
import { usePolledRead } from "../../polledRead.js";
import { Section } from "../../ui/section";
import { readFrameImpediment } from "./api";
import { impedimentWords } from "./model";

/** How often the Frame's Overview reads what its Pi last said about Central's error while shown: as
 * often as the page reads the house's lights. */
const CADENCE_MS = 5000;

/** What the page read: not read yet, the Pi's last word, or a read that failed (the last word kept). */
export interface ImpedimentRead {
  view: FrameImpedimentView | null;
  failed: boolean;
}

const NOT_READ: ImpedimentRead = Object.freeze({ view: null, failed: false });

/** What `frameId`'s Pi last said about Central's error on it, read every CADENCE_MS while `open`. */
export function useFrameImpediment(frameId: string, open: boolean): ImpedimentRead {
  const load = useCallback(async (previous: ImpedimentRead): Promise<ImpedimentRead> => {
    const answer = await readFrameImpediment(frameId);
    return answer.ok ? { view: answer.view, failed: false } : { view: previous.view, failed: true };
  }, [frameId]);
  return usePolledRead(load, { cadenceMs: CADENCE_MS, skip: !open, initial: NOT_READ }).value;
}

export interface FrameCentralErrorProps {
  read: ImpedimentRead;
  /** Each room light's name by its id (none read: the ids are said). */
  names: Readonly<Record<string, string>>;
  /** Needs attention, where each doubt says what to do. */
  attentionHref: string;
  /** House › Lights, where each light is. */
  lightsHref: string;
}

/** The impeded Frame's card: Central's two lines as its Pi draws them, labelled Central's. */
function Impeded({ impediment, names, attentionHref, lightsHref }: {
  impediment: Extract<FrameImpediment, { kind: "impeded" }>;
  names: Readonly<Record<string, string>>;
  attentionHref: string;
  lightsHref: string;
}) {
  const { by } = impediment;
  return (
    <>
      <ProblemCard scope="central" verdict={{ severity: "alarm", label: "Central's error", receipt: null }}
        what={impedimentWords(impediment)} doing={null}
        action={{ label: "See what to do on Needs attention", href: attentionHref }}
        details={<Note>{`Reported for output ${by.output_id}, under grant ${by.grant_id}`}</Note>} />
      <FactRow label="Central's card, line 1" tone="reported">{by.words.line1}</FactRow>
      <FactRow label="Line 2" tone="reported">{by.words.line2}</FactRow>
      <OwnerLinks label="Room lights in doubt:" links={by.lights.map((id) => ({ text: names[id] ?? id, href: lightsHref }))} />
    </>
  );
}

/**
 * The Frame page's **Central's error** (on Overview, apart from Hardware: the Pi's own faults are
 * Hardware's): what the Frame's Pi last said about Central's doubts on it. Impeded: the Pi reports
 * the Frame black because Central can't stand behind a room light it uses, and draws Central's card,
 * whose two lines are shown as the Pi has them, with the room lights in doubt and a link to Needs
 * attention, where each says what to do. Clear, or not reported (an older Pi, or never connected): one
 * line saying so. A read that failed says so and keeps the last word.
 */
export function FrameCentralError({ read, names, attentionHref, lightsHref }: FrameCentralErrorProps) {
  const impediment = read.view?.impediment ?? null;
  return (
    <Section title="Central's error">
      {read.failed && (
        <Note>{impediment === null
          ? "Central did not answer with what this Frame's Pi said about Central's errors."
          : "Central did not answer the last read: this is what its Pi said as last read."}</Note>
      )}
      {impediment === null ? (!read.failed && <Note>Loading…</Note>)
        : impediment.kind === "impeded"
          ? <Impeded impediment={impediment} names={names} attentionHref={attentionHref} lightsHref={lightsHref} />
          : <FactRow tone="reported">{impedimentWords(impediment)}</FactRow>}
    </Section>
  );
}
