import type { Severity } from "../design/tokens";
import { FRAME_ID_PATTERN } from "../frameIds.js";
import { explainPrecedence, isBound, plannedFor, rankedContributions } from "../join.js";
import { Note } from "../patterns/fact-row";
import { OwnerLinks } from "../patterns/link-to-owner";
import { PrecedenceExplanation } from "../PrecedenceExplanation.jsx";
import { ReadinessNotice } from "../ReadinessNotice.jsx";
import { readinessReportForFrame } from "../readinessRecovery.js";
import { formatRoute, sceneCreationRoute } from "../routes.js";
import { Disclosure } from "../ui/disclosure";
import { Section } from "../ui/section";
import { FactLine } from "./fact-line";
import { frameHost, HostHealthLink } from "./host-health-link";
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

/** The Frame's Pi's health in the page's words; the classifier's own words are under Details. */
const PI_WORDS: Record<Severity, string> = {
  ok: "Its Pi is healthy",
  todo: "Its Pi needs a look",
  notice: "Its Pi needs a look",
  alarm: "Its Pi has a problem",
  unknown: "Can't tell how its Pi is",
};

/** What Photo Wall sends to the Frame now, in one sentence. */
function sendingWords(sceneId: string | null, phase: string | null, bound: boolean): string {
  if (!bound) return "No Pi feeds this Frame yet, so Photo Wall sends it nothing. Choose one on the Hardware tab.";
  if (sceneId === null) return "Photo Wall is sending nothing to this Frame now: no Scene is showing or scheduled here.";
  return `Photo Wall is sending the Scene “${sceneId}” to this Frame${phase === "outro" ? ", which is finishing" : ""}.`;
}

/**
 * The Frame page's Overview tab, in plain words: what Photo Wall sends to the Frame now (the
 * Scene planned on top, join.js `plannedFor`, never what the Display shows), which other Scenes
 * wait underneath, how its Pi is (a link to the Pi's Hardware page), whether the Pi reported a
 * problem getting content ready, and how to put content on the Frame. Central's own account
 * (its Runs, their priorities, the Pi's classifier words, the readiness report) is under
 * **Details**.
 */
export function FrameOverview({ snapshot, bootFacts, frameId, hosts }: FrameOverviewProps) {
  const served = snapshot as Runtime | null;
  const frame = (served?.inventory?.frames ?? []).find((candidate) => candidate.id === frameId);
  const bound = isBound(frame);
  const planned = plannedFor(served?.runtime, frameId, bound);
  const underneath = (rankedContributions(served?.runtime ?? {}, frameId) as { scene_id: string }[])
    .slice(1).map((intent) => `“${intent.scene_id}”`);
  const host = frameHost({ snapshot, bootFacts, frameId, hosts });
  const readiness = readinessReportForFrame(snapshot, frameId);
  return (
    <>
      <Section title="Now">
        <Note>{sendingWords(planned.sceneId, planned.phase, bound)}</Note>
        {underneath.length > 0 && <Note>{`Waiting underneath it: ${[...new Set(underneath)].join(", ")}.`}</Note>}
        {host !== null && <OwnerLinks links={[{ text: PI_WORDS[host.severity], severity: host.severity, href: host.href }]} />}
        {readiness !== null && <Note>The Pi reported a problem getting this Frame&apos;s content ready (see Details).</Note>}
        <Disclosure summary="Details">
          <FactLine fact={planned.fact} />
          <HostHealthLink snapshot={snapshot} bootFacts={bootFacts} frameId={frameId} hosts={hosts} />
          <ReadinessNotice snapshot={snapshot} frameId={frameId} />
          <PrecedenceExplanation explanation={explainPrecedence(served?.runtime ?? {}, frameId)} listLabel="Why" />
        </Disclosure>
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
          <>
            <Note>This Frame cannot be chosen in a Scene because of its name.</Note>
            <Disclosure summary="Details">
              <Note>A Scene&apos;s Frame name has 96 characters or fewer and no colon.</Note>
            </Disclosure>
          </>
        )}
      </Section>
    </>
  );
}
