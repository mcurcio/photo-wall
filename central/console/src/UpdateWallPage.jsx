import React, { useCallback, useEffect, useRef, useState } from "react";

import { useConfirm } from "./ConfirmAction.jsx";
import { FactLine } from "./domain/fact-line.tsx";
import { readinessWords, wallOn, wallPrevious } from "./domain/releases/model";
import { fact, words } from "./facts.js";
import { heldReboot, rebootRequest, rebootTarget, sendReboot } from "./fleetCommands.js";
import { useNodeDevice } from "./nodeRead.js";
import { playerPageHref } from "./players.js";
import { sendWallVersion, useReleaseRead, WALL_SCOPE, wallSettled, wallVersionWords } from "./releases.js";
import { formatRoute } from "./routes.js";
import { SectionBoundary } from "./SectionBoundary.jsx";
import {
  downloadWait,
  freezeRollout,
  journeyStep,
  journeyTarget,
  keepCount,
  keepPause,
  keepPlan,
  keepRow,
  nextReboot,
  playerEpoch,
  putConfirmation,
  rolloutMembers,
  rowSettled,
  runningBase,
  SELECT_ALONE,
} from "./updateWall.js";

// The verbs' homes (Part E §25): this page is their client, never their replacement.
const RELEASES = formatRoute({ section: "releases" });
const KEEP_ACTIVE_MS = 5000;
const KEEP_IDLE_MS = 15000;
const NOT_READ = Object.freeze({ read: null, readAt: null, error: null });

/** The steps in the order the operator meets them (§25a). */
const STEP_LABELS = Object.freeze([["choose", "Choose"], ["keeping", "Put on the wall"], ["done", "Done"]]);
const STEP_OF = Object.freeze({ paused: "keeping" });

const pausedWords = (version) => "Paused means this page sends no more reboots. Every Player already boots "
  + `${version} from its next boot: any Player that restarts for any reason, a power cut included, boots it then.`;
// The Put confirmation carries WALL_SCOPE; Choose and Paused, which do not render it, carry `KEEP_SCOPE`.
const KEEP_SCOPE = `These are the Players this console knows. ${WALL_SCOPE}`;
const PUT_UNKNOWN = "Outcome unknown: Central did not answer. The next read of the Players' versions decides; "
  + "rebooting starts only if it shows every Player on this release.";
const HIDDEN = "the tab was hidden; this page sends no reboots while it is not shown";
const KEEP_THEN = "When you confirm, this page reboots these Players one at a time, each after the "
  + "previous one rejoins:";
const COUNT_FACT = fact({ kind: "derived", value: "each Rejoined row is this page's judgement",
  basis: "a later start of its host line than this page's reboot, or a running base that identifies this release, "
    + "and every bound Output's Frame live with no current readiness failure; see each row's Evidence" });

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
    onRead(entry.playerId, { read: node.read, readAt: node.readAt, error: node.error });
  }, [entry.playerId, node.read, node.readAt, node.error, onRead]);
  return null;
}

/**
 * The journey for one target release. Its memory is only what Central cannot serve
 * (updateWall.js); everything else is derived on each render.
 */
function Journey({ tag, skippedIds, snapshot, bootFacts, navigate, say, message }) {
  const releases = useReleaseRead();
  const { open, confirmation } = useConfirm(null);
  const read = releases.read;
  const target = read == null ? null : journeyTarget(read, tag);
  const selected = target !== null && target.deployable && wallOn(read, tag);
  const plan = keepPlan(snapshot, bootFacts);

  // Keep: rolling, the reboots this page sent, the operator's skips, each row's read.
  const [rolling, setRolling] = useState(false);
  const rollingRef = useRef(false);
  rollingRef.current = rolling;
  const rollingSince = useRef(/** @type {number|null} */ (null));
  const [pause, setPause] = useState(/** @type {{reason: string, playerId?: string}|null} */ (null));
  // Skips are the operator's choices, so they live in the URL (§25a); the rollout a
  // confirmation named is frozen here, and rolling reboots only its Players.
  const skipped = new Set(skippedIds);
  const [frozen, setFrozen] = useState(/** @type {ReadonlyArray<import("./updateWall.js").PlanEntry>|null} */ (null));
  const frozenRef = useRef(frozen);
  frozenRef.current = frozen;
  const routeOf = (change = {}) => ({ section: "releases", flow: "update", id: tag, skipped: [...skipped], ...change });
  const sentRef = useRef(/** @type {Map<string, import("./updateWall.js").SentReboot>} */ (new Map()));
  const [, setSentVersion] = useState(0);
  const sendingRef = useRef(false);
  const hooksRef = useRef(new Map());
  const [probeReads, setProbeReads] = useState(/** @type {Record<string, object>} */ ({}));
  // A Put whose answer was lost: the first read started after it settles whether rolling
  // starts (only on that read showing every Player on the target, §6.5).
  const [pendingPut, setPendingPut] = useState(
    /** @type {{named: ReadonlyArray<import("./updateWall.js").PlanEntry>, after: number}|null} */ (null));
  const register = useCallback((playerId, node) => hooksRef.current.set(playerId, node), []);
  const onRead = useCallback((playerId, value) => setProbeReads((previous) => ({ ...previous, [playerId]: value })), []);

  const nowMs = performance.now();
  // No rows until the snapshot the plan comes from is read: no completion is claimed from no reads.
  const members = rolloutMembers(plan, frozen, skipped);
  const rows = selected && snapshot != null ? members.map(({ entry, member }) => {
    const sent = sentRef.current.get(entry.playerId) ?? null;
    return keepRow({ node: probeReads[entry.playerId] ?? NOT_READ, snapshot, playerId: entry.playerId, target,
      running: runningBase(read, entry.playerId), sent, skipped: member === "skipped", outside: member === "outside",
      waitedMs: sent !== null ? nowMs - sent.atMs : rolling && rollingSince.current !== null ? nowMs - rollingSince.current : 0 });
  }) : null;
  const step = journeyStep({ releases: read, rows }, { tag }, { rolling });

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
      const request = rebootRequest(rebootTarget(latest), latest.read?.reboot_commands ?? null,
        snapshot, latest.readAt, { playerId, commandId: crypto.randomUUID(), reason: "update-wall" });
      if ("refused" in request) {
        stop(`${entry.name}: ${request.refused}`, { playerId });
        return;
      }
      const epoch = playerEpoch(snapshot, playerId);
      const result = await sendReboot(entry.deviceId, request, node);
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

  const waiting = target === null || !target.deployable ? null : downloadWait(target);
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
    // The first reboot waits for Central's download (§6.5); the rows stay Waiting meanwhile.
    if (waiting !== null) return;
    const next = nextReboot(rows);
    if (next === null || sendingRef.current || sentRef.current.has(next)) return;
    void rebootNext(next);
    // The rows are compared by their key: a read that changes no row sends nothing new.
  }, [step.step, rowsKey, waiting]);

  const start = () => {
    setPause(null);
    rollingSince.current = performance.now();
    setRolling(true);
  };
  // Rolling starts only over a frozen rollout. Without one (a reload, or a target put on
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

  // A lost Put answer settles on the first read started after it: rolling starts only when
  // that read shows every Player on the target; anything else forgets it.
  useEffect(() => {
    if (pendingPut === null || releases.seq <= pendingPut.after || releases.read === null) return;
    if (wallSettled(tag, releases.read).outcome === "done") {
      setFrozen(pendingPut.named);
      frozenRef.current = pendingPut.named;
      start();
    }
    setPendingPut(null);
  }, [pendingPut, releases.seq, releases.read]);

  // Put vX on the wall (§6.5, R7): ONE confirmation naming the reboot cost, then every Player's
  // version through its one send function. Rolling starts on a done outcome, holds on unknown
  // until the next read settles it, and never starts on changed or refused.
  const onPut = (event) => {
    // The rollout this confirmation names, frozen as named: a Player that appears later is
    // never rebooted.
    const named = freezeRollout(plan, skipped);
    const dialog = putConfirmation(target);
    open(event, {
      key: `journey-put-${tag}`,
      title: dialog.title,
      body: (
        <>
          {dialog.lines.map((line) => <p key={line}>{line}</p>)}
          <PlanNames plan={plan} skipped={skipped} />
          {dialog.after.map((line) => <p key={line}>{line}</p>)}
        </>
      ),
      confirmLabel: dialog.confirmLabel,
      run: async () => {
        setPendingPut(null);
        const outcome = await sendWallVersion(tag, releases);
        if (outcome.outcome === "done") {
          setFrozen(named);
          frozenRef.current = named;
          start();
        } else if (outcome.outcome === "unknown") {
          setPendingPut({ named, after: releases.startedReads() });
        }
        void releases.refresh();
        return { state: outcome.outcome, message: outcome.outcome === "unknown" ? PUT_UNKNOWN : outcome.message };
      },
    });
  };

  // Undo (§6.5): every Player back on the version they all booted before, no reboot. Its own
  // Update the wall then offers the reboots.
  const onUndo = (event) => {
    const previous = wallPrevious(read);
    if (previous === null) {
      say("Undo unavailable: the Players did not all boot one release before.");
      return;
    }
    open(event, {
      key: `journey-undo-${previous}`,
      title: `Put release ${previous} back on every Player?`,
      body: (
        <>
          <p>{WALL_SCOPE}</p>
          <p>{SELECT_ALONE}</p>
        </>
      ),
      confirmLabel: `Put ${previous} back`,
      run: async () => {
        const outcome = await sendWallVersion(previous, releases);
        void releases.refresh();
        if (outcome.outcome === "done") navigate({ section: "releases", flow: "update", id: previous });
        return { state: outcome.outcome, message: outcome.message };
      },
    });
  };

  if (read === null) {
    return releases.error === null ? <p className="player__read-time">Release read: not read yet</p>
      : <FactLine label="Release read" fact={fact({ kind: "unknown", why: `Central did not answer (${releases.error.code})` })} />;
  }
  const putButton = target?.deployable === true && !selected && (
    <button type="button" onClick={onPut}>{`Put ${tag} on the wall…`}</button>
  );

  return (
    <>
      <Steps step={step.step} />
      {message !== null && <p className="roster__status-line" role="alert">{message}</p>}
      {confirmation("roster__status-line")}
      {step.step === "no_release" && (
        <p className="page__empty">{`Central's release catalog does not list release ${tag}. `}<a href={RELEASES}>Releases</a></p>
      )}
      {step.step === "rejected" && (
        <p className="page__empty">{`Central cannot put release ${tag} on the wall: Rejected: `
          + `${words(step.target.release.problem)}. `}<a href={RELEASES}>Releases</a></p>
      )}
      {step.step === "choose" && (
        <SectionBoundary title="Choose" resetKey={releases.readAt}>
          <h3 className="player__layer-title">Put it on the wall</h3>
          {(() => {
            const value = readinessWords(step.target.release);
            return <FactLine label="Download" fact={value === null
              ? fact({ kind: "unknown", why: "Central did not serve whether it is downloaded" }) : fact({ kind: "set", value })} />;
          })()}
          <p className="roster__note">{KEEP_SCOPE}</p>
          <PlanChoice plan={plan} skipped={skipped} onToggle={toggleSkip} />
          {putButton}
        </SectionBoundary>
      )}
      {(step.step === "keeping" || step.step === "paused") && (
        <SectionBoundary title="Put on the wall" resetKey={null}>
          <p className="player__state">{wallVersionWords(tag)}</p>
          <p className="roster__note">{KEEP_SCOPE}</p>
          {step.step === "paused" && (
            <>
              <p className="player__state" role="status">{`Paused · ${keepCount(rows)}`}</p>
              <FactLine label="On this release" fact={COUNT_FACT} />
              {pause !== null && <p className="roster__note">{`Why: ${pause.reason}.`}</p>}
              <p className="roster__note">{pausedWords(tag)}</p>
              {pendingPut !== null && <p className="roster__note" role="status">{PUT_UNKNOWN}</p>}
              <button type="button" onClick={(event) => resume(event)} disabled={pendingPut !== null}>Resume</button>
            </>
          )}
          {step.step === "keeping" && (
            <>
              <p className="player__state" role="status">{`Rebooting one at a time · ${keepCount(rows)}`}</p>
              {waiting !== null && <p className="roster__note">{waiting}</p>}
              <FactLine label="On this release" fact={COUNT_FACT} />
              <button type="button" onClick={() => stop("you stopped it")}>Stop</button>
            </>
          )}
          <KeepRows members={members} rows={rows} snapshot={snapshot} pause={pause} sent={sentRef.current}
            onSkip={skip} onRetry={retry} />
        </SectionBoundary>
      )}
      {step.step === "done" && (
        <SectionBoundary title="Done" resetKey={null}>
          <p className="player__state">{`Done · ${keepCount(rows)}`}</p>
          <FactLine label="On this release" fact={COUNT_FACT} />
          <KeepRows members={members} rows={rows} snapshot={snapshot} pause={null} sent={sentRef.current} />
          {wallPrevious(read) !== null && wallPrevious(read) !== tag && (
            <button type="button" onClick={onUndo}>{`Undo: put release ${wallPrevious(read)} back…`}</button>
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
      <ol aria-label="Players this page reboots">{kept.map((entry) => <li key={entry.playerId}>{planLine(entry)}</li>)}</ol>
      {left.length > 0 && <p>{`Skipped: ${left.map((entry) => entry.name).join(", ")}.`}</p>}
    </>
  );
}

/** Choose: the Keep plan before anything is sent, each Player with Skip (or Include again). */
function PlanChoice({ plan, skipped, onToggle }) {
  if (plan.length === 0) return <p className="roster__note">This console knows no Player to reboot.</p>;
  return (
    <ol className="player__commands" aria-label="Players this page reboots, in order">
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
 * `#/releases/update/<tag>[/skip/<player>…]`.
 * A client of the verbs' homes that adds no Central state and no verb.
 *
 * @param {{tag: string, skipped: string[], snapshot: object|null,
 *          bootFacts: object|null, navigate: (route: import("./routes.js").Route) => void}} props
 */
export function UpdateWallPage({ tag, skipped, snapshot, bootFacts, navigate }) {
  const [message, setMessage] = useState(/** @type {string|null} */ (null));
  return (
    <div className="player">
      <header className="player__header">
        <p><a href={RELEASES}>Releases</a></p>
        <h2 className="player__name">{`Update the wall with release ${tag}`}</h2>
      </header>
      <Journey tag={tag} skippedIds={skipped} snapshot={snapshot} bootFacts={bootFacts} navigate={navigate}
        say={setMessage} message={message} />
    </div>
  );
}
