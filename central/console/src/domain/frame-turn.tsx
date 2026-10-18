import type * as React from "react";
import { useState } from "react";

import { turnFrame } from "../framesApi.js";
import { formatRoute } from "../routes.js";
import { Note } from "../patterns/fact-row";
import { OwnerLinks } from "../patterns/link-to-owner";
import { ProblemCard } from "../patterns/problem-card";
import { Field } from "../ui/field";
import { Section } from "../ui/section";
import { SegmentedControl } from "../ui/segmented-control";
import { useMutate } from "../useMutate.js";
import { type Turn, TURN_WORDS, TURNS } from "./turn-words";

export interface FrameTurnProps {
  frameId: string;
  /** The Frame's turn as Central serves it (the inventory's `turn`). */
  turn: Turn;
  /** The Frame's generation as served: the turn is set against it (Central refuses a stale one). */
  generation: number;
}

/** What the last choice came to: the turn applied, or why not (Central's code, if it answered). */
export type TurnAnswer = { kind: "changed" } | { kind: "refused"; words: string; code: string | null };

export interface FrameTurnChoiceProps {
  /** The turn the control shows: the one being sent while it is sent, else the Frame's. */
  turn: Turn;
  /** A choice is being sent: the control waits for its answer. */
  sending: boolean;
  answer: TurnAnswer | null;
  /** The Frame page's Position tab, where the turned picture's position is checked. */
  positionHref: string;
  onChoose: (turn: Turn) => void;
}

const OPTIONS = TURNS.map((turn) => ({ value: String(turn), label: TURN_WORDS[turn] }));

/** Central's refusal of a turn, in the operator's words. */
function refusal(code: string): string {
  switch (code) {
    case "binding_generation_conflict":
      return "This Frame changed while you chose. Choose again.";
    case "unknown_frame":
      return "This Frame no longer exists.";
    default:
      return "Photo Wall did not take the choice. Choose again.";
  }
}

/**
 * The section as drawn (stories draw it in each state): the four turns as one choice, the answer
 * to the last one under it.
 */
export function FrameTurnChoice({ turn, sending, answer, positionHref, onChoose }: FrameTurnChoiceProps) {
  return (
    <Section title="How the screen hangs">
      <Field label="Screen"
        help="Where the screen's own top edge is, as you face it. Photo Wall turns the picture to match.">
        <SegmentedControl label="Screen" value={String(turn)} options={OPTIONS} disabled={sending}
          onChange={(value) => onChoose(Number(value) as Turn)} />
      </Field>
      {answer?.kind === "changed" ? (
        <>
          <Note live>The picture turns now. Check its position on the Position tab, then press Done there.</Note>
          <OwnerLinks links={[{ text: "Open the Position tab", href: positionHref }]} />
        </>
      ) : null}
      {answer?.kind === "refused" ? (
        <ProblemCard
          scope="setup"
          variant="inline"
          live
          verdict={{ severity: "todo", label: "Not turned", receipt: null }}
          what={answer.words}
          doing={null}
          details={answer.code === null ? undefined : <Note>{`Photo Wall's answer: ${answer.code}`}</Note>}
        />
      ) : null}
    </Section>
  );
}

/**
 * The Hardware tab's "How the screen hangs" (render redesign S5; the Frame's turn, owner gate Q3,
 * 2026-10-17: the turn belongs to the spot on the wall; one Frame per screen, Q4): the four turns
 * in words (turn-words.ts) as one choice, set the moment it is chosen (framesApi.js `turnFrame`,
 * against the generation shown), no Done. A change turns the screen's picture on the Pi and makes
 * the Frame's Position one to check again (Central: readiness position-needed), which the section
 * says with a way to the Position tab. Refusals in words: the Frame changed meanwhile (409
 * binding_generation_conflict), the Frame is gone (404 unknown_frame), no answer.
 */
export function FrameTurn({ frameId, turn, generation }: FrameTurnProps): React.ReactElement {
  const mutate = useMutate();
  const [sending, setSending] = useState<Turn | null>(null);
  const [answer, setAnswer] = useState<TurnAnswer | null>(null);

  const choose = async (chosen: Turn) => {
    if (sending !== null) return;
    setSending(chosen);
    setAnswer(null);
    try {
      const result = await mutate(() => turnFrame(frameId, { turn: chosen, expected_generation: generation })) as
        { ok: boolean; changed?: boolean; code?: string };
      if (result.ok) {
        setAnswer(result.changed ? { kind: "changed" } : null);
      } else {
        const code = result.code ?? "unknown";
        setAnswer({ kind: "refused", words: refusal(code), code });
      }
    } catch (failure) {
      setAnswer({ kind: "refused", words: "Photo Wall's server did not answer. Choose again.",
        code: (failure as Error)?.message ?? null });
    } finally {
      setSending(null);
    }
  };

  return (
    <FrameTurnChoice turn={sending ?? turn} sending={sending !== null} answer={answer}
      positionHref={formatRoute({ section: "wall", id: frameId, tab: "position" })}
      onChoose={(chosen) => { void choose(chosen); }} />
  );
}
