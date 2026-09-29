import React, { useCallback, useId, useRef, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { useConfirm } from "./ConfirmAction.jsx";
import { UNKNOWN_MESSAGE } from "./equipmentApi.js";
import { SummaryCard } from "./flow/SummaryCard.jsx";
import { ChevronIcon } from "./icons.jsx";
import { explainPrecedence, LIVE_PHASES } from "./join.js";
import { WhyNothingNew } from "./MediaPipeline.jsx";
import { PrecedenceExplanation } from "./NowShowingFacet.jsx";
import { SHOW_KEYS } from "./showNowModel.js";
import { ShowNowFlow } from "./ShowNowFlow.jsx";
import { runRows } from "./showState.js";
import { FrameChips } from "./TargetPicker.jsx";
import { useMutate } from "./useMutate.js";

/**
 * The Runs region of the Now showing page (Bead 16; pass 2 slice 3 §9–§11; flow design
 * §7 J7 and "See what is showing and why").
 *
 *  1. SHOW NOW — the Show-now flow (ShowNowFlow.jsx), Scene → Review → "Activate now".
 *     It is always mounted, so its draft and activation key outlive every step and
 *     section change; while its route shows a step, the rest of the region is hidden.
 *  2. RUNS — the served Runs (runtime.current.runs, live plus those ended in the last
 *     day) as cards from showState.js `runRows`: live roots with their child Scenes
 *     nested, then a closed "Recently ended" list. Finish asks for a natural end;
 *     Cancel stops now and goes through ConfirmAction.
 *  3. WHY — per frame, "Why?" opens Central's plan for it (join.js `explainPrecedence`,
 *     the Now-showing facet's explanation) and "Why nothing new?" the media chain
 *     (MediaPipeline.jsx `WhyNothingNew`), each a disclosure.
 *
 * Everything here is Central's plan (intent), never a readback of what a panel shows
 * (R2). Every write wraps the shared `useMutate()` hook (primitive #7), so Plane A
 * refreshes exactly once after it.
 *
 * @param {{snapshot: object|null, route: import("./routes.js").Route|null,
 *          navigate: (route: import("./routes.js").Route, options?: {replace?: boolean}) => void,
 *          recentScene: {sceneId: string, seq: number}|null,
 *          markDraft: (section: string, dirty: boolean) => void}} props
 */
export function RunsRegion({ snapshot, route, navigate, recentScene, markDraft }) {
  const regionRef = useRef(/** @type {HTMLElement|null} */ (null));
  // After a cancel, or when its opener is gone, the Runs region takes focus.
  const focusRegion = () => regionRef.current?.focus();
  const { open, confirmation } = useConfirm(focusRegion, focusRegion);
  const mutate = useMutate();
  const rows = runRows(snapshot);
  const ended = rows.completed.length + rows.cancelled.length;
  const inFlow = SHOW_KEYS.fromRoute(route) !== null;

  const finishRun = useCallback(
    (runId) =>
      mutate(() =>
        apiWrite(`/v1/operator/runs/${encodeURIComponent(runId)}/finish`, {
          method: "POST",
        }),
      ),
    [mutate],
  );

  const actions = {
    onFinish: finishRun,
    onCancel: (event, row) => open(event, cancelRequest(row)),
  };

  return (
    <section
      ref={regionRef}
      tabIndex={-1}
      className="showrunner__region"
      role="region"
      aria-label="Runs"
    >
      <h2 className="showrunner__region-title">Runs</h2>
      <ShowNowFlow
        snapshot={snapshot}
        route={route}
        navigate={navigate}
        recentScene={recentScene}
        markDraft={markDraft}
      />

      <div className="run-cards" hidden={inFlow}>
        <p className="run-control__note">
          Central&apos;s plan: what each frame is meant to show now, not a readback of the panels.
        </p>
        {rows.live.length === 0 ? (
          <p className="run-control__empty">No Run is running.</p>
        ) : (
          <ul className="card-grid run-cards__list" role="list" aria-label="Running Runs">
            {rows.live.map((row) => (
              <RunCard key={row.run.run_id} row={row} snapshot={snapshot} actions={actions} />
            ))}
          </ul>
        )}

        {ended > 0 && (
          <details className="run-control__ended">
            <summary>{`Recently ended (${ended})`}</summary>
            <p className="run-control__note">Runs that ended in the last day.</p>
            <EndedRuns title="Completed" rows={rows.completed} snapshot={snapshot} />
            <EndedRuns title="Cancelled" rows={rows.cancelled} snapshot={snapshot} />
          </details>
        )}

        <WhyPanel snapshot={snapshot} />
      </div>
      {confirmation("run-control__status-line")}
    </section>
  );
}

/**
 * Cancel one Run (§9): captured when the dialog opens. Cancel stops now, skipping the
 * outro, and its children stop too (central/runtime.py `_cancel`).
 */
function cancelRequest(row) {
  const { run } = row;
  const where = row.frames.length > 0 ? row.frames.join(", ") : "its targets";
  return {
    key: `cancel:${run.run_id}`,
    title: `Cancel the Run of ${run.scene_id}?`,
    confirmLabel: "Confirm cancel",
    body: <p>{`Stops now on ${where}, skipping its outro; its child Scenes stop too.`}</p>,
    run: async () => {
      const result = await apiWrite(`/v1/operator/runs/${encodeURIComponent(run.run_id)}/cancel`, {
        method: "POST",
      });
      if (result.ok) {
        return { state: "done", message: `Run of ${run.scene_id} cancelled.` };
      }
      if (result.status >= 500) {
        return { state: "unknown", message: UNKNOWN_MESSAGE };
      }
      return { state: "refused", message: `Not cancelled: ${result.error ?? result.status}.` };
    },
  };
}

/** A Run's chip: its state in a word ("Running", "Finishing"), none once ended. */
function runChip(row) {
  if (!LIVE_PHASES.has(row.run.phase)) {
    return null;
  }
  return row.finishing ? { tone: "todo", text: "Finishing" } : { tone: "ok", text: "Running" };
}

/**
 * One Run as a card (§9; flow design §7): `Scene X`, its state, origin, age, one-cycle
 * wording, priority, protection, revision and its frames with their health. Roots carry
 * Finish and Cancel; their child Scenes are nested beneath as cards of their own.
 */
function RunCard({ row, snapshot, actions = null }) {
  const statusId = useId();
  const { run } = row;
  const lines = [
    { label: "State", value: <span id={statusId}>{row.status}</span> },
    { label: "Origin", value: row.origin },
    { label: "Started", value: row.started },
    ...(row.cycle !== null ? [{ label: "Cycle", value: row.cycle }] : []),
    { label: "Priority", value: `priority ${run.priority}` },
    ...(row.protection !== null ? [{ label: "Protection", value: row.protection }] : []),
    { label: "Revision", value: `revision ${run.scene_revision}` },
    { label: "Frames", value: <FrameChips snapshot={snapshot} frameIds={row.frames} /> },
  ];
  if (row.children.length > 0) {
    lines.push({
      label: "Child Scenes",
      value: (
        <ul className="run-cards__children" aria-label="Child Scenes">
          {row.children.map((child) => (
            <RunCard key={child.run.run_id} row={child} snapshot={snapshot} />
          ))}
        </ul>
      ),
    });
  }
  return (
    <li className="card-grid__item" aria-label={`Run ${run.run_id}`}>
      <SummaryCard
        title={`Scene ${run.scene_id}`}
        chip={runChip(row)}
        lines={lines}
        actions={
          actions === null ? null : (
            <>
              <button
                type="button"
                className="run-control__finish"
                aria-label={`Finish run ${run.run_id}`}
                aria-describedby={row.finishing ? statusId : undefined}
                disabled={row.finishing}
                onClick={() => actions.onFinish(run.run_id)}
              >
                Finish
              </button>
              <button
                type="button"
                className="run-control__cancel"
                aria-label={`Cancel run ${run.run_id}`}
                onClick={(event) => actions.onCancel(event, row)}
              >
                Cancel
              </button>
            </>
          )
        }
      />
    </li>
  );
}

function EndedRuns({ title, rows, snapshot }) {
  if (rows.length === 0) {
    return null;
  }
  return (
    <>
      <h3 className="run-control__ended-title">{title}</h3>
      <ul className="card-grid run-cards__list" aria-label={`${title} Runs`}>
        {rows.map((row) => (
          <RunCard key={row.run.run_id} row={row} snapshot={snapshot} />
        ))}
      </ul>
    </>
  );
}

/**
 * Why each frame shows what Central plans for it (§10) and why nothing new shows there
 * (§14): one row per frame, each with two disclosures. Which are open is kept here
 * (the page never unmounts), and a closed one renders nothing.
 */
function WhyPanel({ snapshot }) {
  const frames = snapshot?.inventory?.frames ?? [];
  const [shown, setShown] = useState(() => new Set());
  const toggle = (key) =>
    setShown((previous) => {
      const next = new Set(previous);
      if (!next.delete(key)) {
        next.add(key);
      }
      return next;
    });
  return (
    <div className="why-frames" role="group" aria-label="Why">
      <h3 className="why-frames__title">Why each frame shows what it does</h3>
      {frames.length === 0 ? (
        <p className="run-control__empty">No frames yet.</p>
      ) : (
        <ul className="why-frames__list" aria-label="Frames">
          {frames.map((frame) => (
            <WhyFrame
              key={frame.id}
              snapshot={snapshot}
              frameId={frame.id}
              open={(part) => shown.has(`${part}:${frame.id}`)}
              onToggle={(part) => toggle(`${part}:${frame.id}`)}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * One frame's row: "Why?" (the precedence list) and "Why nothing new?" (the media
 * chain), each a WAI-ARIA disclosure (a button with `aria-expanded` and
 * `aria-controls`, its panel `hidden` while closed). The chain is its own group beside
 * the ranked list, never inside it (§14).
 */
function WhyFrame({ snapshot, frameId, open, onToggle }) {
  const id = useId();
  const parts = [
    {
      part: "why",
      label: "Why?",
      name: `Why? ${frameId}`,
      render: () => (
        <PrecedenceExplanation
          explanation={explainPrecedence(snapshot?.runtime, frameId)}
          listLabel="Contribution precedence"
          listClass="run-control__why-list"
          emptyClass="run-control__empty"
        />
      ),
    },
    {
      part: "nothing",
      label: "Why nothing new?",
      name: `Why nothing new? ${frameId}`,
      render: () => <WhyNothingNew snapshot={snapshot} frameId={frameId} />,
    },
  ];
  return (
    <li className="why-frames__item">
      <div className="why-frames__head">
        <span className="why-frames__frame">{frameId}</span>
        {parts.map(({ part, label, name }) => (
          <button
            key={part}
            type="button"
            className="advanced__toggle why-frames__toggle"
            aria-label={name}
            aria-expanded={open(part)}
            aria-controls={`${id}-${part}`}
            onClick={() => onToggle(part)}
          >
            <ChevronIcon />
            {label}
          </button>
        ))}
      </div>
      {parts.map(({ part, render }) => (
        <div key={part} id={`${id}-${part}`} className="why-frames__panel" hidden={!open(part)}>
          {open(part) && render()}
        </div>
      ))}
    </li>
  );
}
