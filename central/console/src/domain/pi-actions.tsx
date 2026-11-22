import { useCallback, useEffect, useRef } from "react";
import type * as React from "react";

import { callApi } from "../apiWrite.js";
import { CONSTANTS } from "../generated/contract";
import type { AppRestartsView, AppRestartView, FrameStatus } from "../generated/contract";
import { useNodeDevice } from "../nodeRead.js";
import { Note } from "../patterns/fact-row";
import { ProblemCard } from "../patterns/problem-card";
import { SaveDialog } from "../patterns/save-dialog";
import { type ProgressStepView, StepProgress } from "../patterns/step-progress";
import { usePolledRead } from "../polledRead.js";
import { clockTime, timeOfDay } from "../timeWords.js";
import { Button } from "../ui/button";
import { Inline, Stack } from "../ui/stack";
import { ACTION_WORDS, piReports } from "./frame-status";
import { type PiAsk, piAsks } from "./pi-asks";
import { RESTART_FAILURES, RESTART_ORDER, RESTART_REFUSALS, RESTART_STEPS } from "./restart-words";

export type { PiAsk } from "./pi-asks";

/**
 * The two things Home asks of a Frame's Pi (decision 0036, G3): **Restart app** and **Reboot the
 * Pi**, each acknowledged, never shown as done before the Pi's report, and drawn as `StepProgress`
 * with Central's time for each step. Neither is ever repeated by the console: a step that does not
 * happen says so, with Retry as a new request a person makes.
 *
 * Restart app is Central's `app-restarts` route (central/fleet/app_restart.py): one POST with a
 * request id (a double tap is one request), then its progress read from the same address. Reboot
 * goes through the fleet's ONE send path (fleetCommands.js `sendReboot`, handed in as `AskReboot`
 * by the route: homeCommands.js), confirmed first: it blacks every Frame of the Pi for minutes and
 * cannot be taken back.
 *
 * How far each request got is kept in domain/pi-asks.ts, outside the page, so leaving Home and
 * coming back shows the same steps and asks nothing again.
 */

/** The Pi's node read as the reboot's send path judges it (nodeRead.js `useNodeDevice`). */
type NodeRead = ReturnType<typeof useNodeDevice>;

/**
 * Ask a Pi to reboot through the fleet's one send path, judged on the node read's newest value:
 * null when Central recorded the request, else why not, in words.
 */
export type AskReboot = (request: { deviceId: string; playerId: string | null; requestId: string; node: NodeRead;
  snapshot: object | null }) => Promise<string | null>;

// --- Restart app.

const restartsPath = (deviceId: string) => CONSTANTS.APP_RESTARTS_PATH.replace("{device_id}", encodeURIComponent(deviceId));

type Sent = { ok: true } | { ok: false; error: string };
type RestartRead = { ok: true; view: AppRestartsView } | { ok: false; error: string };

/** Why a request was not taken; a code this console does not know is never shown as it is. */
function refusalWords(error: string): string {
  if (error === "unreachable") return "Central did not answer. Nothing was asked of the Pi.";
  return (RESTART_REFUSALS as Record<string, string | undefined>)[error] ?? "Central did not take the request.";
}

/** A restart's four steps for `StepProgress`: reported ones done with their time, the next one
 * awaited (or failed, where it stopped), the rest not yet. A request Central did not take
 * (`refusedNote`) stopped at its first step: nothing is awaited. */
export function restartSteps(view: AppRestartView | null, failureNote: string | undefined, refusedNote?: string): ProgressStepView[] {
  if (refusedNote !== undefined) {
    return RESTART_ORDER.map((step, index) => (index === 0 ? { label: RESTART_STEPS[step], state: "failed", note: refusedNote }
      : { label: RESTART_STEPS[step], state: "waiting" }));
  }
  const at = new Map((view?.steps ?? []).map((step) => [step.step, step.at]));
  const next = RESTART_ORDER.find((step) => at.get(step) == null) ?? null;
  return RESTART_ORDER.map((step) => {
    const time = at.get(step) ?? null;
    if (time !== null) return { label: RESTART_STEPS[step], state: "done", at: timeOfDay(time) };
    if (step !== next) return { label: RESTART_STEPS[step], state: "waiting" };
    return view?.state === "failed" ? { label: RESTART_STEPS[step], state: "failed", note: failureNote }
      : { label: RESTART_STEPS[step], state: "current" };
  });
}

// How often a request in progress is read again: often enough to see each step as it lands.
const PROGRESS_CADENCE_MS = 2000;
const RESTART_NOT_READ: RestartRead = { ok: true, view: { restart: null } };

export interface PiProgressProps {
  ask: PiAsk;
  /** Read Home again now: something was asked, or a step landed. */
  onChanged: () => void;
  /** Ask again, as a new request. */
  onRetry: (ask: PiAsk) => void;
  /** Put the progress away (finished, failed and read, or cancelled). */
  onClose: (ask: PiAsk) => void;
}

/** Restart app in progress: the request sent once, then its four steps as Central records them.
 * Coming back to it (domain/pi-asks.ts) sends nothing again: it reads where the restart stands. */
function RestartProgress({ ask, onChanged, onRetry, onClose }: PiProgressProps) {
  const { deviceId, requestId, phase } = ask;
  useEffect(() => {
    if (phase !== "sending") return;
    // Not undone when the page goes away: the answer is the store's to keep. The same request id
    // sent twice is one request at Central.
    void (callApi("answer", restartsPath(deviceId), { method: "POST", body: { request_id: requestId } }) as Promise<Sent>)
      .then((answer) => {
        piAsks.update(requestId, answer.ok ? { phase: "waiting" } : { phase: "ended", refused: answer.error });
        onChanged();
      });
  }, [deviceId, requestId, phase, onChanged]);
  const load = useCallback(
    () => callApi("view", restartsPath(deviceId), { method: "GET" }) as Promise<RestartRead>, [deviceId]);
  const taken = phase !== "sending" && ask.refused === null;
  const read = usePolledRead(load, { cadenceMs: PROGRESS_CADENCE_MS, skip: !taken, initial: RESTART_NOT_READ }).value;
  // Only this request's own restart: another one of this Pi is never read as this one's. Central
  // answers the Pi's newest only, so what it last said of this one is kept with the ask.
  const served = read.ok && read.view.restart?.request_id === requestId ? read.view.restart : null;
  useEffect(() => {
    if (served !== null) piAsks.update(requestId, { restart: served });
  }, [served, requestId]);
  const restart = served ?? ask.restart;
  const state = restart?.state ?? "in_progress";
  useEffect(() => {
    if (state === "in_progress") return;
    piAsks.update(requestId, { phase: "ended" });
    onChanged();
  }, [state, requestId, onChanged]);
  const failure = restart?.failure != null ? RESTART_FAILURES[restart.failure] : undefined;
  const refused = ask.refused !== null ? refusalWords(ask.refused) : null;
  return (
    <ProgressCard title={`Restarting the photo app on ${ask.pi}`} ask={ask} onClose={onClose}
      closable={refused !== null || state !== "in_progress"}
      steps={restartSteps(restart, failure, refused ?? undefined)}
      problem={refused !== null || state === "failed" ? (
        <ProblemCard scope="action" variant="inline" subject={ask.pi} live
          verdict={{ severity: "alarm", label: "Not restarted", receipt: null }}
          what={refused ?? failure ?? "The restart did not finish."}
          doing="Photo Wall does not try again by itself."
          action={{ label: "Retry", onAction: () => onRetry(ask) }} />
      ) : undefined} />
  );
}

// --- Reboot the Pi.

interface RebootRecord {
  command_id: string;
  requested_at: number;
  delivered: boolean | null;
  answered_at: number | null;
}

/** How long a Pi may take to come back, on Central's clock, before the progress says it did not:
 * Central's own number for a reboot on its way (central/frame_status/policy.py). */
const REBOOT_WAIT_SECONDS = CONSTANTS.REBOOT_ALLOWED_SECONDS;

const REBOOT_STEPS = { requested: "Requested", accepted: "Accepted by the Pi", back: "Showing again" } as const;

/**
 * A reboot's three steps: Central's record of the request, the Pi's answer to it, and its Frames
 * reporting again after having stopped (`back`: Central's time of it, or null). `readAt` is
 * Central's time of the read, the only clock the deadline is judged on.
 */
export function rebootSteps(record: RebootRecord | null, back: number | null, readAt: number | null): ProgressStepView[] {
  const requested: ProgressStepView = record === null ? { label: REBOOT_STEPS.requested, state: "current" }
    : { label: REBOOT_STEPS.requested, state: "done", at: timeOfDay(record.requested_at) };
  if (record === null) {
    return [requested, { label: REBOOT_STEPS.accepted, state: "waiting" }, { label: REBOOT_STEPS.back, state: "waiting" }];
  }
  if (record.delivered === false) {
    return [requested,
      { label: REBOOT_STEPS.accepted, state: "failed", note: "the Pi has no connection to Central, or it did not answer." },
      { label: REBOOT_STEPS.back, state: "waiting" }];
  }
  if (record.delivered !== true) {
    return [requested, { label: REBOOT_STEPS.accepted, state: "current" }, { label: REBOOT_STEPS.back, state: "waiting" }];
  }
  const accepted: ProgressStepView = { label: REBOOT_STEPS.accepted, state: "done",
    at: record.answered_at === null ? null : timeOfDay(record.answered_at) };
  if (back !== null) return [requested, accepted, { label: REBOOT_STEPS.back, state: "done", at: timeOfDay(back) }];
  const deadline = record.requested_at + REBOOT_WAIT_SECONDS;
  return [requested, accepted, readAt !== null && readAt > deadline
    ? { label: REBOOT_STEPS.back, state: "failed", note: `no report from its Frames by ${clockTime(deadline)}.` }
    : { label: REBOOT_STEPS.back, state: "current" }];
}

export interface RebootProgressProps extends PiProgressProps {
  /** The console's snapshot: the reboot request names the Pi's Frames from it (fleetCommands.js). */
  snapshot: object | null;
  /** The status Central serves for each of the Pi's Frames now. */
  statuses: readonly (FrameStatus | null)[];
  /** Where the Pi's own page is, for a reboot that did not come back. */
  piHref: string;
  askReboot: AskReboot;
}

/** Reboot the Pi: confirmed first, sent through the fleet's one send path, then its steps. */
function RebootProgress({ ask, snapshot, statuses, piHref, askReboot, onChanged, onClose }: RebootProgressProps) {
  const node = useNodeDevice(ask.deviceId, { cadenceMs: PROGRESS_CADENCE_MS });
  // Nothing is sent before the dialog is answered (domain/pi-asks.ts `confirming`).
  const sent = ask.phase !== "confirming";
  // Set the moment the request is taken, before the dialog's own close: closing it then keeps the ask.
  const taken = useRef(false);
  // Whether the Pi's Frames were seen not reporting since the request: only then does "reporting"
  // mean they came back, never that the reboot has not started yet. Kept with the ask, so it is
  // still known after Home was left and shown again.
  const { seenDown, requestId } = ask;
  const reporting = statuses.length > 0 && statuses.every((status) => piReports(status));
  const listed = (node.read as { reboot_commands?: RebootRecord[] } | null)?.reboot_commands ?? [];
  const record = listed.find((command) => command.command_id === ask.requestId) ?? null;
  const accepted = record?.delivered === true;
  const wentDown = sent && accepted && !reporting && !seenDown;
  useEffect(() => {
    if (wentDown) piAsks.update(requestId, { seenDown: true });
  }, [wentDown, requestId]);
  const back = sent && accepted && seenDown && reporting
    ? Math.max(...statuses.map((status) => status?.since ?? 0)) : null;
  const done = back !== null;
  useEffect(() => {
    if (done) onChanged();
  }, [done, onChanged]);

  const send = async (): Promise<string | null> => {
    const refused = await askReboot({ deviceId: ask.deviceId, playerId: ask.playerId, requestId: ask.requestId, node, snapshot });
    if (refused !== null) return refused;
    taken.current = true;
    piAsks.update(requestId, { phase: "waiting" });
    void node.refresh();
    onChanged();
    return null;
  };

  if (!sent) {
    return (
      <SaveDialog title={`Reboot ${ask.pi}?`} action={ACTION_WORDS["reboot-pi"]} danger onSave={send}
        onClose={() => { if (!taken.current) onClose(ask); }}>
        {() => (
          <Note>
            {ask.frameNames.length > 0
              ? `Every Frame this Pi feeds goes dark for a few minutes while it restarts: ${ask.frameNames.join(", ")}. It cannot be taken back.`
              : "The Pi restarts, which takes a few minutes. It cannot be taken back."}
          </Note>
        )}
      </SaveDialog>
    );
  }
  const steps = rebootSteps(record, back, node.readAt);
  const failed = steps.find((step) => step.state === "failed");
  return <RebootSteps ask={ask} steps={steps} failed={failed} done={done} piHref={piHref} onClose={onClose} />;
}

/** A sent reboot's steps; once it is back or stopped, the request has ended. */
function RebootSteps({ ask, steps, failed, done, piHref, onClose }: {
  ask: PiAsk; steps: ProgressStepView[]; failed: ProgressStepView | undefined; done: boolean; piHref: string;
  onClose: (ask: PiAsk) => void;
}) {
  const ended = done || failed !== undefined;
  const { requestId } = ask;
  useEffect(() => {
    if (ended) piAsks.update(requestId, { phase: "ended" });
  }, [ended, requestId]);
  return (
    <ProgressCard title={`Rebooting ${ask.pi}`} ask={ask} onClose={onClose} closable={done || failed !== undefined}
      steps={steps}
      problem={failed !== undefined ? (
        <ProblemCard scope="action" variant="inline" subject={ask.pi} live
          verdict={{ severity: "alarm", label: "Not back", receipt: null }}
          what={`The reboot stopped at “${failed.label}”: ${failed.note ?? ""}`}
          doing="Photo Wall does not try again by itself."
          action={{ label: "Open the Pi", href: piHref }} />
      ) : undefined} />
  );
}

// --- Both.

interface ProgressCardProps {
  title: string;
  ask: PiAsk;
  steps: readonly ProgressStepView[];
  problem?: React.ReactNode;
  closable: boolean;
  onClose: (ask: PiAsk) => void;
}

/** One request's progress: its steps, its problem if it stopped, and Close once it has ended. */
function ProgressCard({ title, ask, steps, problem, closable, onClose }: ProgressCardProps) {
  return (
    <Stack label={title}>
      <Note>{title}</Note>
      <StepProgress label={title} steps={steps} />
      {problem}
      {closable && <Inline><Button variant="ghost" onClick={() => onClose(ask)}>Close</Button></Inline>}
    </Stack>
  );
}

/** One thing asked of a Pi, in progress. */
export function PiProgress(props: RebootProgressProps) {
  const { ask, snapshot, statuses, piHref, askReboot, onChanged, onRetry, onClose } = props;
  return ask.kind === "restart-app"
    ? <RestartProgress ask={ask} onChanged={onChanged} onRetry={onRetry} onClose={onClose} />
    : <RebootProgress ask={ask} snapshot={snapshot} statuses={statuses} piHref={piHref} askReboot={askReboot}
      onChanged={onChanged} onRetry={onRetry} onClose={onClose} />;
}

/** What a tile says while its Pi has a request Central took and the Pi has not finished: "Restart requested". */
export const REQUESTED_WORDS: Record<PiAsk["kind"], string> = {
  "restart-app": "Restart requested",
  "reboot-pi": "Reboot requested",
};
