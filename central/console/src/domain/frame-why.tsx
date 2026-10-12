import { useCallback } from "react";

import { readHouse } from "../houseApi.js";
import { Note } from "../patterns/fact-row";
import { ProblemCard } from "../patterns/problem-card";
import { usePolledRead } from "../polledRead.js";
import { houseLocal, untilWords } from "../timeWords.js";
import { readFrameWhy } from "../whyApi.js";
import { Section } from "../ui/section";

type Power = "on" | "off";

/** A request for content on a Frame (central/why/model.py `ContentRequest`). */
export interface ContentRequest {
  kind: "program" | "show-now" | "default-scene";
  activation_id: string;
  run_id: string | null;
  scene_id: string;
  program_id: string | null;
  name: string;
  rank: number;
}

/** GET /v1/operator/frames/{id}/why (central/why/model.py `FrameWhy`). */
export interface FrameWhyView {
  frame_id: string;
  content: {
    winner: ContentRequest | null;
    beat: readonly { request: ContentRequest; because: "lower-priority" | "admitted-earlier" | "kept-out-by-protected-frames" }[];
  };
  power: {
    wanted: Power;
    because: "night-off" | "hold" | "test" | "on-underneath";
    until: string | null;
    pi_says: { kind: "test" | "timetable" | "timetable-ran-out"; power: Power | null } | "not-heard";
    /** The Pi's last power result for the change in force; null: not heard, or none needed. */
    result: "confirmed" | "signal-stopped" | "did-not-answer" | "not-supported" | "another-input" | null;
    /** Central's Resting (central/why/explain.py `is_resting`, its one home). */
    resting: boolean;
  } | null;
}

/** One request in words: a Schedule by its name, the Default Scene's Scene, or Show now's. */
function requestWords(request: ContentRequest): string {
  if (request.kind === "program") return request.name;
  if (request.kind === "default-scene") return `${request.scene_id}, the Default Scene`;
  return `Show now: ${request.scene_id}`;
}

const BECAUSE: Record<"lower-priority" | "admitted-earlier", string> = {
  "lower-priority": "which has lower priority",
  "admitted-earlier": "which started earlier",
};

/**
 * What plays on the Frame and what it beat (E6, moment 7): "Evenings is playing; it beat calm,
 * the Default Scene, which has lower priority." A request a protected Scene kept out is named
 * after it.
 */
export function contentWhyWords(content: FrameWhyView["content"]): string {
  if (content.winner === null) {
    const kept = content.beat.filter((beaten) => beaten.because === "kept-out-by-protected-frames");
    return kept.length === 0 ? "Nothing is playing here: no Schedule, Show now or Default Scene covers this Frame now."
      : `Nothing is playing here; ${kept.map((beaten) => requestWords(beaten.request)).join(", ")} kept out by a protected Scene.`;
  }
  const beat = content.beat.map((beaten) => beaten.because === "kept-out-by-protected-frames"
    ? `${requestWords(beaten.request)} kept out by a protected Scene`
    : `it beat ${requestWords(beaten.request)}, ${BECAUSE[beaten.because]}`);
  return `${requestWords(content.winner)} is playing${beat.length === 0 ? "" : `; ${beat.join("; ")}`}.`;
}

const REASON: Record<"night-off" | "test", string> = { "night-off": "Night off", test: "a power test" };

/**
 * The Frame's display power in one line (the amended U1), from Central's answer only: Resting is
 * Central's `resting`; a display that did not answer, or that another input keeps on, is a power
 * problem; "Not heard from the Pi" while no report answers Central's latest document.
 */
export function powerWhyWords(power: NonNullable<FrameWhyView["power"]>, now: string | null): string {
  const until = power.until === null ? null : untilWords(power.until, now);
  if (power.result === "did-not-answer") return "Display didn't answer";
  if (power.wanted === "off" && power.result === "another-input") return "Kept on: the display is showing another input";
  if (power.pi_says === "not-heard") return "Not heard from the Pi";
  if (power.pi_says.kind === "timetable-ran-out") return "Left as it is: no power times from Central for a week";
  if (power.because === "hold") {
    return `Held ${power.wanted} by the console${until === null ? " until you resume" : ` until ${until}`}`;
  }
  if (power.wanted === "off") {
    if (!power.resting) return "Turning off: the Pi has not carried out the off yet";
    return `Resting: ${REASON[power.because === "test" ? "test" : "night-off"]}${until === null ? "" : ` until ${until}`}`;
  }
  return until === null ? "On" : `On: Night off starts at ${until}`;
}

/** The Frame's Why as read (null: not yet). */
export type WhyRead = { ok: true; why: FrameWhyView; zone: string | null } | { ok: false; error: string } | null;
type Read = WhyRead;

/** The Frame's Why and the house zone, re-read while shown and on `refresh` (a tab shown). */
export function useFrameWhy(frameId: string) {
  const load = useCallback(async (): Promise<Read> => {
    const [why, house] = await Promise.all([readFrameWhy(frameId), readHouse()]) as [
      { ok: true; why: FrameWhyView } | { ok: false; error: string },
      { ok: true; settings: { time: { zone: string } } } | { ok: false },
    ];
    if (!why.ok) return why;
    return { ok: true, why: why.why, zone: house.ok ? house.settings.time.zone : null };
  }, [frameId]);
  const { value, refresh } = usePolledRead(load, { cadenceMs: 10000, initial: null as Read });
  return { read: value, refresh };
}

export interface FrameWhyProps {
  read: Read;
  /** Central's read time (the snapshot's), for "until 07:00". */
  readAt: number | null;
  /** Only the power line (the Power tab). */
  powerOnly?: boolean;
}

/** "Why is this playing?" on the Frame page's Overview (E6), and its power line on the Power tab. */
export function FrameWhy({ read, readAt, powerOnly = false }: FrameWhyProps) {
  if (read === null) return <Note>Reading why…</Note>;
  if (!read.ok) {
    return (
      <ProblemCard scope="central" variant="inline" verdict={{ severity: "unknown", label: "Can't tell", receipt: null }}
        what="Central did not answer with why this Frame plays what it does." doing="The page tries again by itself."
        details={<Note>{`Central's answer: ${read.error}`}</Note>} />
    );
  }
  const now = readAt === null || read.zone === null ? null : houseLocal(readAt, read.zone);
  const power = read.why.power === null ? "Display power: no Pi feeds this Frame yet."
    : `Display power: ${powerWhyWords(read.why.power, now)}`;
  if (powerOnly) return <Note>{power}</Note>;
  return (
    <Section title="Why is this playing?">
      <Note>{contentWhyWords(read.why.content)}</Note>
      <Note>{power}</Note>
    </Section>
  );
}
