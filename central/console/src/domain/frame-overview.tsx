import { FRAME_ID_PATTERN } from "../frameIds.js";
import { explainPrecedence, isBound, plannedFor } from "../join.js";
import { Note } from "../patterns/fact-row";
import { OwnerLinks } from "../patterns/link-to-owner";
import { PrecedenceExplanation } from "../PrecedenceExplanation.jsx";
import { ReadinessNotice } from "../ReadinessNotice.jsx";
import { formatRoute, sceneCreationRoute } from "../routes.js";
import { Section } from "../ui/section";
import { FactLine } from "./fact-line";
import { HostHealthLink } from "./host-health-link";
import type { HostsRead } from "./hosts-read";

export interface FrameOverviewProps {
  snapshot: object | null;
  bootFacts: object | null;
  frameId: string;
  hosts: HostsRead | null;
}

interface Runtime {
  runtime?: object | null;
  inventory?: { frames?: { id: string }[] };
}

/**
 * The Frame page's Overview tab: what Central's Runs put on this Frame now (the `planned` fact,
 * join.js `plannedFor`, never what the Display shows), why (each Run on it and its priority),
 * the Pi's host health as a link to its Hardware page, the Player's last readiness report, and
 * how to put content on the Frame.
 */
export function FrameOverview({ snapshot, bootFacts, frameId, hosts }: FrameOverviewProps) {
  const served = snapshot as Runtime | null;
  const frame = (served?.inventory?.frames ?? []).find((candidate) => candidate.id === frameId);
  const planned = plannedFor(served?.runtime, frameId, isBound(frame));
  return (
    <>
      <Section title="Now">
        <FactLine fact={planned.fact} />
        {planned.phase === "outro" && <Note>Ending (outro)</Note>}
        <HostHealthLink snapshot={snapshot} bootFacts={bootFacts} frameId={frameId} hosts={hosts} />
        <ReadinessNotice snapshot={snapshot} frameId={frameId} />
      </Section>
      <Section title="Why this is on the Frame">
        <PrecedenceExplanation explanation={explainPrecedence(served?.runtime ?? {}, frameId)} listLabel="Why" />
      </Section>
      <Section title="Put content on this Frame">
        <Note>
          {`Make a Scene; Frame ${frameId} starts selected on its Frames step. The Scene chooses the photos or videos; after saving it, choose Show now or Schedule it.`}
        </Note>
        {FRAME_ID_PATTERN.test(frameId) ? (
          <OwnerLinks links={[
            { text: "Make a Scene", href: formatRoute(sceneCreationRoute(frameId) as never) },
            { text: "Browse Scenes", href: formatRoute({ section: "scenes" }) },
          ]} />
        ) : (
          <Note>This Frame&apos;s id cannot be a Scene target: a target id has 96 characters or fewer and no colon.</Note>
        )}
      </Section>
    </>
  );
}
