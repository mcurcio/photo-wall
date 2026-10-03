import React, { useCallback, useEffect, useRef, useState } from "react";

import { useConfirm } from "./ConfirmAction.jsx";
import { FactLine } from "./FactLine.jsx";
import { fact } from "./facts.js";
import {
  appOperationState,
  heldReboot,
  rebootCommandState,
  rebootRequest,
  rebootTarget,
  sendReboot,
} from "./fleetCommands.js";
import { frameHealth } from "./health.js";
import { liveRunsFor } from "./join.js";
import { effectGateFact, NodeRecords, nodeReadsAllowed, useNodeControlValue } from "./nodeControl.js";
import { currentSessionBoot, useNodeDevice } from "./nodeRead.js";
import { playerPageHref } from "./players.js";
import { QualifiedFallback } from "./QualifiedFallback.jsx";
import {
  deploymentHandle,
  PUBLISH_HELD_WORDS,
  publishConfirmation,
  publishOffer,
  publishRequest,
  releaseHome,
  selectionConfirmation,
  selectionRequest,
  sendSelection,
  useReleaseRead,
} from "./releases.js";
import { useHeldRequests } from "./sendOutcome.js";
import { formatRoute } from "./routes.js";
import { SectionBoundary } from "./SectionBoundary.jsx";
import { BoundRule } from "./StageApp.jsx";
import {
  pendingStage,
  sendStage,
  stageBlocker,
  stageReplaces,
  stageRequest,
  stageScope,
  useHeldStage,
} from "./stage.js";
import {
  freezeRollout,
  journeyStep,
  journeyTarget,
  keepCount,
  keepPause,
  keepPlan,
  keepRow,
  nextReboot,
  playerEpoch,
  rolloutMembers,
  rowSettled,
  stageFollowUp,
  tryWithdrawn,
} from "./updateWall.js";

// The verbs' homes (Part E §25): this page is their client, never their replacement.
const RELEASES = formatRoute({ section: "releases" });
const SHOW_NOW = formatRoute({ section: "now", flow: "show", step: "scene" });
const KEEP_ACTIVE_MS = 5000;
const KEEP_IDLE_MS = 15000;
const NOT_READ = Object.freeze({ read: null, operations: null, readAt: null, error: null });

/** The steps in the order the operator meets them (§25a); Try is optional. */
const STEP_LABELS = Object.freeze([
  ["get_it", "Get it"], ["choose", "Choose"], ["qualifying", "Qualify (if needed)"], ["staging", "Try on one Frame"],
  ["looking", "Look"], ["keeping", "Keep"], ["done", "Done"],
]);
// Back out branches from Look; only journeyStep's done (the boot ended or interrupted the stage) is Done.
const STEP_OF = Object.freeze({ paused: "keeping", backing_out: "looking" });

const pausedWords = (deploymentId) => "Paused means this page sends no more reboots. Select is fleet-wide: any "
  + `Player that restarts for any reason, a power cut included, is offered deployment ${deploymentHandle(deploymentId)} at that boot.`;
// The Keep dialog shows only KNOWN_PLAYERS: `selectionConfirmation` beside it carries the fleet-wide claim. Choose and
// Paused, which do not render it, carry the full `keepScope`.
const KNOWN_PLAYERS = "These are the Players this console knows.";
const keepScope = (deploymentId) => `${KNOWN_PLAYERS} Select is fleet-wide: any other Pi `
  + `that boots by node path is offered deployment ${deploymentHandle(deploymentId)} at its next boot.`;
const GATE_CLOSED_KEEP = "Each Player is offered it at its next boot; this page cannot reboot them while the gate is closed.";
const HIDDEN = "the tab was hidden; this page sends no reboots while it is not shown";
const KEEP_THEN = "Then, when you press Start rebooting, this page reboots these Players one at a time, each after the "
  + "previous one rejoins:";
const FALLBACK_LOOK = "The new app did not start; the Player fell back on its own. Back out is recommended.";
const COUNT_FACT = fact({ kind: "derived", value: "each Rejoined row is this page's judgement",
  basis: "a later kernel boot than this page's reboot, or a linked app that identifies this release, and every bound "
    + "Output's Frame live with no current readiness failure; see each row's Evidence" });

const gateOpen = (gate) => gate?.effective_state === "open";

/** The step strip: where the journey is, re-derived from the reads on every render. */
function Steps({ step }) {
  const current = STEP_OF[step] ?? step;
  return (
    <ol className="journey__steps" aria-label="Steps">
      {STEP_LABELS.map(([key, label]) => (
        <li key={key} aria-current={key === current ? "step" : undefined}>{label}</li>
      ))}
    </ol>
  );
}

/** Get it (§25a): the release, and Publish through its one send function. */
function GetIt({ read, target, held, releases, open }) {
  const { release, withApp } = target;
  const offer = publishOffer(read, release, withApp, held);
  const choice = withApp ? "with its app" : "without its app";
  // The verb's own confirmation (releases.js `publishConfirmation`): its words and its send.
  const publish = (event, again) => {
    const request = again ? held.frozen(offer.deploymentId) : publishRequest(read, release, withApp, held);
    if (request === null || "refused" in request) return;
    const { lines, ...dialog } = publishConfirmation(request, again, { releases, held });
    open(event, {
      ...dialog,
      key: `journey-publish-${again ? "again-" : ""}${request.body.deployment_id}`,
      body: <>{lines.map((line) => <p key={line}>{line}</p>)}</>,
    });
  };
  const row = releaseHome(read).releases.find((entry) => entry.release.manifest_sha256 === release.manifest_sha256);
  return (
    <>
      <FactLine label="Release" fact={row.catalog} />
      <FactLine label="Contents" fact={row.contents} />
      {offer.offer === "publish" && (
        <button type="button" onClick={(event) => publish(event, false)}>{`Publish ${choice}…`}</button>
      )}
      {offer.offer === "blocked" && <p className="roster__note">{`Publish unavailable: ${offer.reason}.`}</p>}
      {Object.hasOwn(PUBLISH_HELD_WORDS, offer.offer) && (
        <p className="player__state" role="status">{PUBLISH_HELD_WORDS[offer.offer]}</p>
      )}
      {offer.offer === "unknown" && (
        <button type="button" onClick={(event) => publish(event, true)}>Send publish again…</button>
      )}
    </>
  );
}

/** The bound Frames of the tried Player, each with its health and live Runs (snapshot facts). */
function FramesNow({ snapshot, frames }) {
  return (
    <ul className="player__commands" aria-label="Frames this Player drives">
      {frames.map(({ frameId }) => {
        const runs = liveRunsFor(snapshot?.runtime, frameId);
        return (
          <li key={frameId} className="player__command">
            <p className="player__state">{`Frame ${frameId}: ${frameHealth(snapshot, frameId)?.label ?? "not in the inventory"}`}</p>
            <p className="roster__note">{runs.length === 0 ? "No live Run"
              : `Live Run ${runs.map((run) => `${run.scene_id} (${run.phase})`).join(", ")}`}</p>
          </li>
        );
      })}
    </ul>
  );
}

/** One app operation's named state and evidence (fleetCommands.js `appOperationState`). */
function OperationLine({ operation, readAt }) {
  if (operation == null) return null;
  const state = appOperationState(operation, readAt);
  return (
    <>
      <p className="player__state">{state.label}</p>
      <FactLine label="Evidence" fact={state.fact} />
    </>
  );
}

/**
 * One Keep row's node read: mounted per planned Player while the target is selected, each its
 * own polled read (5 s for the active row, 15 s otherwise), reported up to the page.
 */
function KeepProbe({ entry, active, register, onRead }) {
  const node = useNodeDevice(entry.deviceId, { cadenceMs: active ? KEEP_ACTIVE_MS : KEEP_IDLE_MS });
  useEffect(() => {
    register(entry.playerId, node);
  });
  useEffect(() => {
    onRead(entry.playerId, { read: node.read, operations: node.operations, readAt: node.readAt, error: node.error });
  }, [entry.playerId, node.read, node.operations, node.readAt, node.error, onRead]);
  return null;
}

/**
 * The journey for one target release and one tried Player (or none). Its memory is only what
 * Central cannot serve (updateWall.js); everything else is derived on each render.
 */
function Journey({ tag, tried, skippedIds, snapshot, bootFacts, navigate, say, message, withdrawnBy, withdraw }) {
  const control = useNodeControlValue();
  const allowed = nodeReadsAllowed(control);
  const releases = useReleaseRead({ skip: !allowed });
  const heldPublishes = useHeldRequests();
  const heldStage = useHeldStage();
  const { open, confirmation } = useConfirm(null);
  const read = releases.read;
  const target = read == null ? null : journeyTarget(read, tag);
  const selected = target !== null && target.listed && read.selection?.deployment_id === target.deploymentId;
  const plan = keepPlan(snapshot, bootFacts, tried);
  const triedEntry = tried == null ? null : plan.find((entry) => entry.playerId === tried && entry.frames.length > 0) ?? null;
  const triedNode = useNodeDevice(triedEntry?.deviceId ?? "none", { skip: !allowed || triedEntry === null || selected });

  const [memory, setMemory] = useState({ sampling: false, qualified: false, needQualify: false, refusals: 0,
    backingOut: false, stageId: null });
  const remember = (change) => setMemory((previous) => ({ ...previous, ...change }));
  const [stageWords, setStageWords] = useState(/** @type {string|null} */ (null));
  const stageIds = useRef(/** @type {{operationId: string, commandId: string}|null} */ (null));
  const [backOut, setBackOut] = useState(/** @type {object|null} */ (null));
  // Back out's in-flight hold: set synchronously before the await, so a second click before the
  // first answer lands sends nothing (as `sendStage`'s held "in_flight" and `rebootNext`'s sendingRef).
  const backOutSending = useRef(false);
  const [backOutInFlight, setBackOutInFlight] = useState(false);

  // Keep: rolling, the reboots this page sent, the operator's skips, each row's read.
  const [rolling, setRolling] = useState(false);
  const rollingRef = useRef(false);
  rollingRef.current = rolling;
  const rollingSince = useRef(/** @type {number|null} */ (null));
  const [pause, setPause] = useState(/** @type {{reason: string, playerId?: string, gate?: true}|null} */ (null));
  // Skips are the operator's choices, so they live in the URL (§25a); the rollout a
  // confirmation named is frozen here, and rolling reboots only its Players.
  const skipped = new Set(skippedIds);
  const [frozen, setFrozen] = useState(/** @type {ReadonlyArray<import("./updateWall.js").PlanEntry>|null} */ (null));
  const frozenRef = useRef(frozen);
  frozenRef.current = frozen;
  const routeOf = (change = {}) => ({ section: "releases", flow: "update", id: tag,
    ...(tried == null ? {} : { tried }), skipped: [...skipped], ...change });
  const sentRef = useRef(/** @type {Map<string, import("./updateWall.js").SentReboot>} */ (new Map()));
  const [, setSentVersion] = useState(0);
  const sendingRef = useRef(false);
  const hooksRef = useRef(new Map());
  const [probeReads, setProbeReads] = useState(/** @type {Record<string, object>} */ ({}));
  const previousSelection = useRef(/** @type {string|null} */ (null));
  // Keep's Select landed on this page and no reboot has been started yet: Paused offers Start.
  const selectedHere = useRef(false);
  const [started, setStarted] = useState(false);
  const register = useCallback((playerId, node) => hooksRef.current.set(playerId, node), []);
  const onRead = useCallback((playerId, value) => setProbeReads((previous) => ({ ...previous, [playerId]: value })), []);

  const nowMs = performance.now();
  // No rows until the snapshot the plan comes from is read: no completion is claimed from no reads.
  const members = rolloutMembers(plan, frozen, skipped);
  const rows = selected && snapshot != null ? members.map(({ entry, member }) => {
    const sent = sentRef.current.get(entry.playerId) ?? null;
    return keepRow({ node: probeReads[entry.playerId] ?? NOT_READ, snapshot, playerId: entry.playerId, target,
      gate: control.gate, sent, skipped: member === "skipped", outside: member === "outside",
      waitedMs: sent !== null ? nowMs - sent.atMs : rolling && rollingSince.current !== null ? nowMs - rollingSince.current : 0 });
  }) : null;
  const step = journeyStep({ releases: read, tried: triedEntry === null || selected ? null : { operations: triedNode.operations }, rows },
    { tag, tried: triedEntry?.playerId ?? null }, { ...memory, rolling });

  const stop = useCallback((reason, extra = {}) => {
    setRolling(false);
    setPause({ reason, ...extra });
  }, []);

  // Rolling advances only while the tab is visible: hiding it pauses the rollout.
  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState !== "visible" && rollingRef.current) stop(HIDDEN);
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, [stop]);

  // The one reboot this page sends next (updateWall.js `nextReboot`), through `sendReboot`.
  const rebootNext = async (playerId) => {
    // Only a Player of the frozen rollout: nothing else is ever rebooted by this page.
    const entry = frozenRef.current?.find((candidate) => candidate.playerId === playerId);
    const node = hooksRef.current.get(playerId);
    if (entry === undefined || node === undefined) return;
    sendingRef.current = true;
    try {
      const latest = node.latest();
      const request = rebootRequest(rebootTarget(latest, control.latest().gate), latest.read?.reboot_commands ?? null,
        snapshot, latest.readAt, { playerId, commandId: crypto.randomUUID(), reason: "update-wall" });
      if ("refused" in request) {
        stop(`${entry.name}: ${request.refused}`, { playerId });
        return;
      }
      const epoch = playerEpoch(snapshot, playerId);
      const result = await sendReboot(entry.deviceId, request, node, control);
      if (heldReboot(request, result) !== null) {
        sentRef.current.set(playerId, { request, atMs: performance.now(), epoch });
        setSentVersion((version) => version + 1);
      }
      if (result.outcome !== "done" && result.outcome !== "already") stop(`${entry.name}: ${result.message}`, { playerId });
      void node.refresh();
    } finally {
      sendingRef.current = false;
    }
  };

  const rowsKey = rows === null ? "" : rows.map((entry) => `${entry.playerId}:${entry.state}:${entry.pause ?? ""}`).join("|");
  useEffect(() => {
    if (step.step !== "keeping" || rows === null) return;
    if (frozenRef.current === null) {
      stop("no confirmation has named the Players to reboot");
      return;
    }
    const why = keepPause(rows);
    if (why !== null) {
      stop(`${members.find(({ entry }) => entry.playerId === why.playerId)?.entry.name ?? "A Player"}: ${why.reason}`, why);
      return;
    }
    const next = nextReboot(rows);
    if (next === null || sendingRef.current || sentRef.current.has(next)) return;
    void rebootNext(next);
    // The rows are compared by their key: a read that changes no row sends nothing new.
  }, [step.step, rowsKey]);

  const start = () => {
    setStarted(true);
    setPause(null);
    rollingSince.current = performance.now();
    setRolling(true);
  };
  // Rolling starts only over a frozen rollout. Without one (a reload, or a target selected
  // elsewhere), Resume first confirms, naming the Players it will reboot, in order.
  const resume = (event) => {
    if (frozenRef.current !== null) {
      start();
      return;
    }
    const named = freezeRollout(plan, skipped);
    open(event ?? null, {
      key: `journey-resume-${named.map((entry) => entry.playerId).join(",")}`,
      title: "Reboot these Players one at a time?",
      body: (
        <>
          <p>{KEEP_THEN}</p>
          <PlanNames plan={plan} skipped={skipped} />
          <p>A Player not named here is never rebooted by this page.</p>
        </>
      ),
      confirmLabel: "Reboot one at a time",
      run: async () => {
        setFrozen(named);
        frozenRef.current = named;
        start();
        return { state: "done", message: "Rebooting one at a time" };
      },
    });
  };
  const retry = (playerId) => {
    sentRef.current.delete(playerId); // re-derived from Central's reads; its outstanding fence still holds
    setSentVersion((version) => version + 1);
    resume();
  };
  // A skip replaces the URL (no history step); its hashchange is dispatched synchronously, so
  // the new route lands in the same render as a resume below.
  const skip = (playerId) => {
    if (!skipped.has(playerId)) navigate(routeOf({ skipped: [...skipped, playerId] }), { replace: true });
    if (pause?.playerId === playerId) resume();
  };
  // Choose: the operator may skip any Player before Keep sends anything, and take it back.
  const toggleSkip = (playerId) => navigate(routeOf({
    skipped: skipped.has(playerId) ? [...skipped].filter((id) => id !== playerId) : [...skipped, playerId],
  }), { replace: true });

  // Keep (§25a): Select through its one send function; rolling starts when it lands.
  const onKeep = (event) => {
    const request = selectionRequest(read, target.deploymentId);
    if ("refused" in request) {
      say(`Keep unavailable: ${request.refused}.`);
      return;
    }
    const canReboot = gateOpen(control.gate);
    // The rollout this confirmation names, frozen as named: a Player that appears later is
    // never rebooted.
    const named = freezeRollout(plan, skipped);
    open(event, {
      key: `journey-keep-${target.deploymentId}-${request.body.expected_revision}`,
      title: `Keep release ${tag} on the wall?`,
      body: (
        <>
          {selectionConfirmation(request).map((line) => <p key={line}>{line}</p>)}
          <p>{KNOWN_PLAYERS}</p>
          <p>{canReboot ? KEEP_THEN : GATE_CLOSED_KEEP}</p>
          {canReboot && <PlanNames plan={plan} skipped={skipped} />}
        </>
      ),
      confirmLabel: "Select for every boot",
      run: async () => {
        // Select alone: no reboot is sent until the operator, shown the plan, presses Start.
        previousSelection.current = releases.latest().read?.selection?.deployment_id ?? null;
        const outcome = await sendSelection(request, releases);
        if (outcome.outcome === "done" || outcome.outcome === "already") {
          selectedHere.current = true;
          if (canReboot) {
            setFrozen(named);
            frozenRef.current = named;
          }
        }
        void releases.refresh();
        return { state: outcome.outcome, message: outcome.message };
      },
    });
  };

  // Try (§25a): stage the target on the tried Player through `sendStage`.
  const onStage = async () => {
    const pending = pendingStage(heldStage.get(), triedNode.operations);
    stageIds.current ??= { operationId: crypto.randomUUID(), commandId: crypto.randomUUID() };
    const request = pending?.state === "unknown" ? pending.request
      : stageRequest(triedNode, control.gate, target.deploymentId, stageIds.current);
    if ("refused" in request) {
      setStageWords(`Stage unavailable: ${request.refused}.`);
      return;
    }
    const outcome = await sendStage(triedEntry.deviceId, request, { node: triedNode, control }, heldStage);
    void triedNode.refresh();
    // Keyed on Central's served code (updateWall.js `stageFollowUp`), never on the words.
    const next = stageFollowUp(outcome, memory.refusals);
    if (next === "stay") {
      setStageWords(outcome.message);
      return;
    }
    stageIds.current = null;
    setStageWords(null);
    if (next === "held") {
      remember({ stageId: request.body.operation_id });
    } else if (next === "qualify") {
      remember({ needQualify: true, qualified: false, refusals: 1 });
      setStageWords(outcome.message);
    } else {
      if (next === "withdraw") withdraw(outcome.message);
      say(outcome.message);
      navigate(routeOf({ tried: undefined }));
    }
  };

  const onQualifyPhase = (phase, words) => {
    if (phase === "sampling") remember({ sampling: true });
    if (phase === "accepted") remember({ sampling: false, qualified: true, needQualify: false });
    if (phase === "stopped") {
      say(words);
      navigate(routeOf({ tried: undefined }));
    }
  };

  // Back out (§25a): reboot the tried Player through `sendReboot`; no selection is sent.
  const onBackOut = async () => {
    if (backOutSending.current) return;
    const latest = triedNode.latest();
    const request = backOut ?? rebootRequest(rebootTarget(latest, control.latest().gate), latest.read?.reboot_commands ?? null,
      snapshot, latest.readAt, { playerId: tried, commandId: crypto.randomUUID(), reason: "update-wall-back-out" });
    if ("refused" in request) {
      say(`Back out unavailable: ${request.refused}.`);
      return;
    }
    backOutSending.current = true;
    setBackOutInFlight(true);
    try {
      const result = await sendReboot(triedEntry.deviceId, request, triedNode, control);
      void triedNode.refresh();
      setBackOut(heldReboot(request, result));
      if (result.outcome === "done" || result.outcome === "already") remember({ backingOut: true });
      else say(result.message);
    } finally {
      backOutSending.current = false;
      setBackOutInFlight(false);
    }
  };

  if (read === null) {
    return releases.error === null ? <p className="player__read-time">Release read: not read yet</p>
      : <FactLine label="Release read" fact={fact({ kind: "unknown", why: `Central did not answer (${releases.error.code})` })} />;
  }
  const gate = control.gate;
  const gateLine = !gateOpen(gate) && (
    <div className="roster__note">
      <FactLine label="Effect gate" fact={effectGateFact(gate)} />
      <a href={RELEASES}>See Releases › Effect gate</a>
    </div>
  );
  const triedName = triedEntry?.name ?? "";

  const keepButton = target?.listed && !selected && (
    <button type="button" onClick={onKeep}>
      {gateOpen(gate) ? "Keep: put it on every Player…" : "Keep: select it for every boot…"}
    </button>
  );

  return (
    <>
      <Steps step={step.step} />
      {message !== null && <p className="roster__status-line" role="alert">{message}</p>}
      {confirmation("roster__status-line")}
      {step.step === "no_release" && (
        <p className="page__empty">{`Central's release catalog does not list release ${tag}. `}<a href={RELEASES}>Releases</a></p>
      )}
      {step.step === "get_it" && (
        <SectionBoundary title="Get it" resetKey={releases.readAt}>
          <GetIt read={read} target={step.target} held={heldPublishes} releases={releases} open={open} />
        </SectionBoundary>
      )}
      {step.step === "choose" && (
        <SectionBoundary title="Choose" resetKey={releases.readAt}>
          {tried != null && triedEntry === null && (
            <p className="roster__note">{`Player ${tried} is not a bound Player this console knows; choose another.`}</p>
          )}
          <h3 className="player__layer-title">Try it on one Frame (optional)</h3>
          {(() => {
            const why = withdrawnBy ?? tryWithdrawn(read, step.target);
            if (why !== null) return <p className="roster__note">{`Try unavailable: ${why}.`}</p>;
            if (!gateOpen(gate)) return <>{"Try unavailable while the effect gate is closed."}{gateLine}</>;
            const triable = plan.filter((entry) => entry.frames.length > 0);
            if (triable.length === 0) return <p className="roster__note">No Player drives a Frame.</p>;
            return (
              <ul className="player__commands" aria-label="Players to try it on">
                {triable.map((entry) => (
                  <li key={entry.playerId}>
                    <a href={formatRoute(routeOf({ tried: entry.playerId }))}>
                      {`Try on ${entry.name} (Frames ${entry.frames.map((frame) => frame.frameId).join(", ")})`}
                    </a>
                  </li>
                ))}
              </ul>
            );
          })()}
          <h3 className="player__layer-title">Keep</h3>
          <p className="roster__note">{keepScope(step.target.deploymentId)}</p>
          <PlanChoice plan={plan} skipped={skipped} onToggle={toggleSkip} />
          {keepButton}
          {!gateOpen(gate) && <p className="roster__note">{GATE_CLOSED_KEEP}</p>}
        </SectionBoundary>
      )}
      {step.step === "qualifying" && triedEntry !== null && (
        <SectionBoundary title="Qualify" resetKey={null}>
          <p className="roster__note">
            {`${triedName} needs a qualified fallback first: one steady photo or looping video on every Output of this `
              + "Player for 30 s. "}
            <a href={SHOW_NOW}>Show now</a>
            {` on Frames ${triedEntry.frames.map((frame) => frame.frameId).join(", ")}.`}
          </p>
          {stageWords !== null && <p className="roster__status-line" role="status">{stageWords}</p>}
          <QualifiedFallback deviceId={triedEntry.deviceId} node={triedNode} snapshot={snapshot} playerId={tried}
            onPhase={onQualifyPhase} />
        </SectionBoundary>
      )}
      {step.step === "staging" && triedEntry !== null && (
        <SectionBoundary title="Try on one Frame" resetKey={null}>
          <p>{stageScope(read.selection)}</p>
          {(() => {
            const replaces = stageReplaces(triedNode.operations);
            return replaces !== null && step.stage == null ? <p>{replaces}</p> : null;
          })()}
          <BoundRule />
          <FramesNow snapshot={snapshot} frames={triedEntry.frames} />
          {step.stage != null ? (
            <OperationLine operation={step.stage} readAt={triedNode.operations?.read_at ?? null} />
          ) : (() => {
            const blocker = stageBlocker(triedNode, triedNode.operations,
              gate, snapshot?.inventory?.players?.find((player) => player.id === tried) ?? null);
            const held = pendingStage(heldStage.get(), triedNode.operations);
            return (
              <>
                <button type="button" onClick={onStage}
                  disabled={(blocker !== null && held?.state !== "unknown") || (held !== null && held.state !== "unknown")}>
                  {held?.state === "unknown" ? "Send the same stage again" : `Stage release ${tag} on ${triedName}`}
                </button>
                {blocker !== null && held === null && (
                  <p className="roster__note">
                    {`Stage unavailable: ${blocker.reason}.`}
                    {blocker.gate === true && <>{" "}<a href={RELEASES}>See Releases › Effect gate</a></>}
                  </p>
                )}
              </>
            );
          })()}
          {stageWords !== null && <p className="roster__status-line" role="status">{stageWords}</p>}
          <p><a href={formatRoute(routeOf({ tried: undefined }))}>Choose again</a></p>
        </SectionBoundary>
      )}
      {step.step === "looking" && triedEntry !== null && (
        <SectionBoundary title="Look" resetKey={null}>
          <p>{`Look at the Frames ${triedName} drives.`}</p>
          <FramesNow snapshot={snapshot} frames={triedEntry.frames} />
          <OperationLine operation={step.stage} readAt={triedNode.operations?.read_at ?? null} />
          {step.stage.state === "fallback_running" && <p className="roster__note" role="alert">{FALLBACK_LOOK}</p>}
          {keepButton}
          <button type="button" onClick={onBackOut} disabled={backOutInFlight}>{backOut !== null ? "Send the same back-out reboot again" : `Back out: reboot ${triedName}`}</button>
          <p className="roster__note">{`Back out reboots ${triedName}; its next boot is offered the boot selection. Nothing is selected.`}</p>
        </SectionBoundary>
      )}
      {step.step === "backing_out" && triedEntry !== null && (
        <SectionBoundary title="Back out" resetKey={null}>
          {(() => {
            const record = backOut === null ? undefined
              : (triedNode.read?.reboot_commands ?? []).find((entry) => entry.command_id === backOut.body.command_id);
            const boot = currentSessionBoot(triedNode);
            return (
              <>
                <p className="player__state">{record === undefined ? "Reboot sent; Central has not listed it yet"
                  : rebootCommandState(record, triedNode.readAt, { nodeDevice: triedNode, gate }).label}</p>
                <FactLine label="Current node session's boot" fact={boot.fact ?? boot.none} />
              </>
            );
          })()}
        </SectionBoundary>
      )}
      {(step.step === "keeping" || step.step === "paused") && (
        <SectionBoundary title="Keep" resetKey={null}>
          <FactLine label="Boot selection" fact={releaseHome(read).selection} />
          <p className="roster__note">{keepScope(step.target.deploymentId)}</p>
          {step.step === "paused" && (
            <>
              <p className="player__state" role="status">{`Paused · ${keepCount(rows)}`}</p>
              <FactLine label="On the selection" fact={COUNT_FACT} />
              {pause !== null && <p className="roster__note">{`Why: ${pause.reason}.`}</p>}
              <p className="roster__note">{pausedWords(step.target.deploymentId)}</p>
              {gateOpen(gate) ? (
                <button type="button" onClick={(event) => resume(event)}>
                  {selectedHere.current && !started ? "Start rebooting" : "Resume"}
                </button>
              )
                : <><p className="roster__note">{GATE_CLOSED_KEEP}</p>{gateLine}</>}
            </>
          )}
          {step.step === "keeping" && (
            <>
              <p className="player__state" role="status">{`Rebooting one at a time · ${keepCount(rows)}`}</p>
              <FactLine label="On the selection" fact={COUNT_FACT} />
              <button type="button" onClick={() => stop("you stopped it")}>Stop</button>
            </>
          )}
          <KeepRows members={members} rows={rows} snapshot={snapshot} pause={pause} sent={sentRef.current}
            onSkip={skip} onRetry={retry} />
        </SectionBoundary>
      )}
      {step.step === "done" && (
        <SectionBoundary title="Done" resetKey={null}>
          {step.how === "backed_out" ? (
            <FactLine label="Backed out" fact={fact({ kind: "derived", value: `${triedName}'s Stage ended by a later boot`,
              basis: "its stage reads interrupted or ended by a later boot; that boot is offered the boot selection, and "
                + "which app it runs is the Player's fact" })} />
          ) : (
            <>
              <p className="player__state">{`Done · ${keepCount(rows)}`}</p>
              <FactLine label="On the selection" fact={COUNT_FACT} />
              <KeepRows members={members} rows={rows} snapshot={snapshot} pause={null} sent={sentRef.current} />
              {(() => {
                const previous = releaseHome(read).releases.find((entry) => entry.deploymentId !== null
                  && entry.deploymentId === previousSelection.current);
                return previous === undefined ? null : (
                  <a href={formatRoute({ section: "releases", flow: "update", id: previous.release.tag })}>
                    {`Undo: update the wall back to release ${previous.release.tag}`}
                  </a>
                );
              })()}
            </>
          )}
        </SectionBoundary>
      )}
      {selected && members.map(({ entry }, index) => (
        <KeepProbe key={entry.playerId} entry={entry} register={register} onRead={onRead}
          active={rows !== null && rows.findIndex((candidate) => !rowSettled(candidate)) === index} />
      ))}
    </>
  );
}

const planLine = (entry) => `${entry.name}${entry.frames.length === 0 ? " (drives no Frame)"
  : ` (Frames ${entry.frames.map((frame) => frame.frameId).join(", ")})`}`;

/** The Keep plan named in the Keep confirmation, in reboot order, the skipped ones apart. */
function PlanNames({ plan, skipped }) {
  const kept = plan.filter((entry) => !skipped.has(entry.playerId));
  const left = plan.filter((entry) => skipped.has(entry.playerId));
  return (
    <>
      <ol aria-label="Players Keep reboots">{kept.map((entry) => <li key={entry.playerId}>{planLine(entry)}</li>)}</ol>
      {left.length > 0 && <p>{`Skipped: ${left.map((entry) => entry.name).join(", ")}.`}</p>}
    </>
  );
}

/** Choose: the Keep plan before anything is sent, each Player with Skip (or Include again). */
function PlanChoice({ plan, skipped, onToggle }) {
  if (plan.length === 0) return <p className="roster__note">This console knows no Player to reboot.</p>;
  return (
    <ol className="player__commands" aria-label="Players Keep reboots, in order">
      {plan.map((entry) => {
        const out = skipped.has(entry.playerId);
        return (
          <li key={entry.playerId} className="player__command">
            <p>{`${planLine(entry)}${out ? " · Skipped" : ""}`}</p>
            <button type="button" onClick={() => onToggle(entry.playerId)}>
              {out ? `Include ${entry.name}` : `Skip ${entry.name}`}
            </button>
          </li>
        );
      })}
    </ol>
  );
}

/** The Keep plan, one row per Player: its state, a link to its home, and Skip or Retry. */
function KeepRows({ members, rows, snapshot, pause, sent, onSkip, onRetry }) {
  return (
    <ul className="player__commands" aria-label="Players to keep it on">
      {members.map(({ entry }, index) => {
        const row = rows[index];
        const href = playerPageHref(snapshot, entry.playerId);
        return (
          <li key={entry.playerId} className="player__command" aria-label={entry.name}>
            <p>{href === null ? entry.name : <a href={href}>{entry.name}</a>}</p>
            <p className="player__state">{row.label}</p>
            {row.evidence !== undefined && <FactLine label="Evidence" fact={row.evidence} />}
            {onSkip && row.state === "waiting" && !sent.has(entry.playerId) && (
              <button type="button" onClick={() => onSkip(entry.playerId)}>{`Skip ${entry.name}`}</button>
            )}
            {onRetry && pause?.playerId === entry.playerId && (
              <>
                <button type="button" onClick={() => onRetry(entry.playerId)}>{`Retry ${entry.name}`}</button>
                {row.state !== "skipped" && (
                  <button type="button" onClick={() => onSkip(entry.playerId)}>{`Skip ${entry.name}`}</button>
                )}
              </>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/**
 * Fleet › Releases › Update the wall (Part E §25a, bead NU1):
 * `#/releases/update/<tag>[/try/<player>][/skip/<player>…]`.
 * A client of the verbs' homes that adds no Central state and no verb. The words of a stopped
 * step survive a change of the tried Player (the journey below is keyed on it).
 *
 * @param {{tag: string, tried: string|null, skipped: string[], snapshot: object|null,
 *          bootFacts: object|null, navigate: (route: import("./routes.js").Route) => void}} props
 */
export function UpdateWallPage({ tag, tried, skipped, snapshot, bootFacts, navigate }) {
  const [message, setMessage] = useState(/** @type {string|null} */ (null));
  const [withdrawn, setWithdrawn] = useState(/** @type {string|null} */ (null));
  return (
    <div className="player">
      <header className="player__header">
        <p><a href={RELEASES}>Releases</a></p>
        <h2 className="player__name">{`Update the wall with release ${tag}`}</h2>
      </header>
      <NodeRecords>
        <Journey key={tried ?? ""} tag={tag} tried={tried} skippedIds={skipped} snapshot={snapshot} bootFacts={bootFacts} navigate={navigate}
          say={setMessage} message={message} withdrawnBy={withdrawn} withdraw={setWithdrawn} />
      </NodeRecords>
    </div>
  );
}
