import { useCallback, useEffect, useMemo, useState } from "react";
import type * as React from "react";

import { type FixView, framePlaces, type HomeHeld, statusOf } from "../domain/frame-status";
import { HomeBoard, type HomeState } from "../domain/home-board";
import { type HostIncident, piFaultView, readinessView } from "../domain/home-reports";
import { LibraryHealthCard } from "../domain/library-health-card";
import { type PiAsk, PiProgress, REQUESTED_WORDS, type AskReboot } from "../domain/pi-actions";
import { piAsks, usePiAsks, waitingAsks } from "../domain/pi-asks";
import { SourcesInDoubt, type SourcesRead, sourcesToShow } from "../domain/room-lights/sources-in-doubt";
import type { Action, FramePlace, Override } from "../generated/contract";
import { playerHandle } from "../health.js";
import { hostIncidents } from "../hostHealth.js";
import { Note } from "../patterns/fact-row";
import { ProblemCard } from "../patterns/problem-card";
import { formatRoute, isPlainClick } from "../routes.js";
import { sinceTime } from "../timeWords.js";
import { Link } from "../ui/link";

/** What Home needs of the console's snapshot besides Home's own document. */
interface HomeSnapshot extends HomeHeld {
  /** Central's time of the snapshot read. */
  readAt?: number;
  /** The newest read of Home's document failed: what is held is the last one that answered. */
  homeFailed?: boolean;
  /** Central's time of the last read of Home's document that answered. */
  homeReadAt?: number | null;
}

type Sent = { ok: true } | { ok: false; error: string };

/**
 * Home's writes, handed in by its route (homeCommands.js): each is its owner's own route. The page
 * and its domain components name no write themselves, but Restart app's request (domain/pi-actions.tsx).
 */
export interface HomeCommands {
  hold: (power: "on" | "off", requestId: string) => Promise<Sent>;
  resumeSchedule: (frameIds: string[]) => Promise<Sent>;
  checkConnection: (connectionRef: string) => Promise<Sent>;
  askReboot: AskReboot;
}

/** A Frame whose photo app reported a failure to prepare what it was given (readinessRecovery.js). */
export interface ReadinessReport {
  frameId: string;
  messages: readonly string[];
  age: string | null;
}

export interface HomePageProps {
  snapshot: HomeSnapshot | null;
  /** The newest snapshot read failed: what is shown is the last one that answered. */
  refreshFailed: boolean;
  /** Read the snapshot, Home's document with it, now: after every action. */
  refresh: () => Promise<unknown>;
  commands: HomeCommands;
  /** The shell's fleet host read and boot facts: the Pis' own faults, and their names. */
  hosts: { read: object | null; failed: boolean } | null;
  bootFacts: object | null;
  /** The photo library connections, read by the route (null: not read yet). */
  library: { connections: readonly React.ComponentProps<typeof LibraryHealthCard>["connection"][] | null } | null;
  /** The room lights Central is in doubt of, read by the route, and the lights' names. */
  sources: SourcesRead;
  lightNames: Readonly<Record<string, string>>;
  readiness: readonly ReadinessReport[];
  /** A tile or a link to a Frame was followed: its page takes focus. */
  onVisitFrame: (frameId: string) => void;
}

const uuid = () => crypto.randomUUID();

/** Why Central did not take a press, in words; its code is Central's own and is never shown. */
function notChanged(error: string): string {
  return error === "unreachable" ? "Not changed: Central did not answer. Press it again."
    : "Not changed: Central did not take it. Read the page again, then press it again.";
}

/**
 * Home (`#/home`, T1; decision 0036): the console's landing. It shows Home's one document (read
 * with the snapshot, every 5 s and at once after an action: finding F2) through `HomeBoard`, and
 * owns only what a press starts: All off and All on, Resume schedule, a problem's fix, and what is
 * asked of a Pi (Restart app, Reboot the Pi), each in progress as `StepProgress`. What was asked of
 * a Pi is kept outside the page (domain/pi-asks.ts), so it is still in progress here after
 * another section was shown.
 *
 * ONE list of what is wrong: Central's problems first, worst first, then what the console's other
 * reads found and Central's status does not carry yet: a Pi's own faults (the fleet host read),
 * the room lights Central is in doubt of, a photo library connection that needs a look, and the
 * photo app's reports of what it could not prepare. A fault Central's problems already name is
 * not said twice.
 */
export function HomePage({
  snapshot, refreshFailed, refresh, commands, hosts, bootFacts, library, sources, lightNames, readiness, onVisitFrame,
}: HomePageProps) {
  const home = snapshot?.home ?? null;
  const now = snapshot?.readAt ?? null;
  const asks = usePiAsks();
  // A reboot whose dialog was left open is not a request: it goes when Home does.
  useEffect(() => () => piAsks.dropUnsent(), []);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const changed = useCallback(() => { void refresh().catch(() => {}); }, [refresh]);

  const places = useMemo(() => framePlaces(home), [home]);
  const pis = useMemo(() => Object.fromEntries(places.flatMap((place) => (place.device_id == null || place.player_id == null ? []
    : [[place.device_id, `Pi ${playerHandle(snapshot, bootFacts, place.player_id)}`] as const]))),
  [places, snapshot, bootFacts]);
  const names = useMemo(() => Object.fromEntries(places.map((place) => [place.frame_id, place.name])), [places]);

  const state: HomeState = snapshot === null ? { kind: "loading" }
    : home === null ? (snapshot.homeFailed === true || refreshFailed ? { kind: "error" } : { kind: "loading" })
      : { kind: "ready", home,
        staleSince: refreshFailed ? snapshot.readAt ?? null : snapshot.homeFailed === true ? snapshot.homeReadAt ?? null : null };

  const ask = (kind: PiAsk["kind"], deviceId: string) => {
    const fed = places.filter((place) => place.device_id === deviceId);
    piAsks.begin({
      kind, deviceId, playerId: fed[0]?.player_id ?? null, pi: pis[deviceId] ?? "its Pi",
      frameIds: fed.map((place) => place.frame_id), frameNames: fed.map((place) => place.name),
    });
  };
  const close = useCallback((done: PiAsk) => piAsks.close(done.requestId), []);
  const retry = useCallback((failed: PiAsk) => piAsks.retry(failed.requestId), []);

  const press = async (send: () => Promise<Sent>, taken: string) => {
    setBusy(true);
    setSaid(null);
    const answer = await send();
    setSaid(answer.ok ? taken : notChanged(answer.error));
    await refresh().catch(() => {});
    setBusy(false);
  };
  const onHold = (power: "on" | "off") => void press(() => commands.hold(power, uuid()), `Holding every display ${power}.`);
  const onResume = (override: Override) => void press(() => commands.resumeSchedule([...override.frame_ids]), "Schedule resumed.");
  const onFix = (fix: NonNullable<FixView["fix"]>) => {
    if (fix.kind === "restart-app" || fix.kind === "reboot-pi") {
      ask(fix.kind, fix.device_id);
    } else if (fix.kind === "resume-schedule") {
      const held = home?.overrides.find((override) => override.id === fix.hold_id);
      if (held !== undefined) onResume(held);
    } else if (fix.kind === "check-now") {
      void press(() => commands.checkConnection(fix.connection_ref), "Check requested. Its answer shows here when it lands.");
    }
  };
  const onAction = (place: FramePlace, action: Action) => {
    if (place.device_id != null) ask(action, place.device_id);
  };

  // Only what Central took and the Pi has not finished: never before a reboot is confirmed, nor
  // after a request finished, failed or was refused (P4: a badge states only what is true).
  const requested = Object.fromEntries(waitingAsks(asks).map((held) => [held.deviceId, REQUESTED_WORDS[held.kind]]));
  const frameLink = (frameId: string) => (
    <Link href={formatRoute({ section: "wall", id: frameId, tab: "overview" })}>{names[frameId] ?? frameId}</Link>
  );

  // What Central's problems already say is not said again by another read.
  const problems = home?.house.problems ?? [];
  const pisNamed = new Set(problems.flatMap((problem) => {
    const device = places.find((place) => place.frame_id === problem.frame_id)?.device_id;
    return problem.part === "pi" && device != null ? [device] : [];
  }));
  const checked = new Set(problems.flatMap((problem) => (problem.fix?.kind === "check-now" ? [problem.fix.connection_ref] : [])));
  const hostsRead = hosts === null || hosts.failed ? null : hosts.read;
  const incidents = (hostIncidents(snapshot, hostsRead, bootFacts) as readonly HostIncident[])
    .filter((incident) => !pisNamed.has(incident.deviceId))
    .map((incident) => ({ incident, view: piFaultView(incident, pis[incident.deviceId] ?? null,
      incident.frames.map((frameId) => names[frameId] ?? frameId)) }));
  const looks = (library?.connections ?? []).filter((connection) => connection.health.state === "needs_a_look"
    && !checked.has(connection.connection_ref));
  const more = incidents.length > 0 || sourcesToShow(sources) || looks.length > 0 || readiness.length > 0 ? (
    <>
      {incidents.map(({ incident, view }) => (
        <ProblemCard key={view.key} scope="setup" subject={view.subject} verdict={view.verdict} what={view.what} doing={null}
          action={{ label: "Open the Pi", href: incident.hardwareHref }}
          details={view.details === null ? undefined : <Note>{view.details}</Note>} />
      ))}
      <SourcesInDoubt read={sources} names={lightNames} since={(at) => sinceTime(at)} frameLink={frameLink}
        lightsHref={formatRoute({ section: "lights" })} />
      {looks.map((connection) => <LibraryHealthCard key={connection.connection_ref} connection={connection} />)}
      {readiness.map((report) => {
        const view = readinessView(report, names[report.frameId] ?? null);
        return (
          <ProblemCard key={view.key} scope="setup" subject={view.subject} verdict={view.verdict} what={view.what} doing={null}
            action={{ label: "Open the Frame", href: formatRoute({ section: "wall", id: report.frameId, tab: "overview" }) }} />
        );
      })}
    </>
  ) : undefined;

  const progress = asks.length === 0 ? undefined : (
    <>
      {asks.map((held) => (
        <PiProgress key={held.requestId} ask={held} snapshot={snapshot} askReboot={commands.askReboot}
          statuses={held.frameIds.map((frameId) => statusOf(home, frameId))}
          piHref={formatRoute({ section: "hardware", id: held.deviceId })}
          onChanged={changed} onRetry={retry} onClose={close} />
      ))}
    </>
  );

  return (
    <HomeBoard state={state} now={now} pis={pis} requested={requested} busy={busy} said={said}
      onFix={onFix} onAction={onAction} onHold={onHold} onResume={onResume}
      onOpenFrame={(frameId, event) => { if (isPlainClick(event)) onVisitFrame(frameId); }}
      progress={progress} more={more} />
  );
}
