import type { Severity } from "../../design/tokens";
import type { PlayerDrawnView } from "../../generated/contract";
import { FactRow } from "../../patterns/fact-row";
import { DRAWN_WORDS } from "./model";

/** The drawn state's band: up to date is no judgement; any other is something to look at. */
const BAND: Record<PlayerDrawnView["state"], Severity | null> = {
  drawn: null, behind: "notice", "not-following": "notice", "not-reported": "unknown",
};

/**
 * DrawnLine: whether a Pi draws its Frames with the house's lights as they are now, as Central
 * judged its last report ("Behind", "Not following", "Not reported"). `drawn` undefined: Central
 * has no report of it yet, said as not reported.
 */
export function DrawnLine({ drawn }: { drawn: PlayerDrawnView | undefined }) {
  const state = drawn?.state ?? "not-reported";
  return (
    <FactRow label="House's lights" tone="reported" band={BAND[state]}>{DRAWN_WORDS[state]}</FactRow>
  );
}
