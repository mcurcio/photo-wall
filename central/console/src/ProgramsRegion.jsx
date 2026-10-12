import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { useConfirm } from "./ConfirmAction.jsx";
import { UNKNOWN_MESSAGE } from "./sendOutcome.js";
import { useProblems } from "./Field.jsx";
import { FlowFrame } from "./flow/FlowFrame.jsx";
import { editKey, editedId } from "./flow/instance.js";
import { sameValue } from "./flow/draftState.js";
import { inStepOrder } from "./flow/steps.js";
import { SummaryCard } from "./flow/SummaryCard.jsx";
import { useFlowDraft } from "./flow/useFlowDraft.js";
import { useFlowInstance, useFlowRefs } from "./flow/useFlowInstance.js";
import { NOT_CONFIRMED, useFlowWrite } from "./flow/useFlowWrite.js";
import { useSceneHandOver } from "./flow/useSceneHandOver.js";
import {
  buildProgram,
  NEW_PROGRAM_DRAFT,
  programDraftProblems,
  programEditDraft,
  REFUSAL_FIELDS,
  SCHEDULE_ADVANCED_FIELDS,
  SCHEDULE_FIELD_STEP,
  SCHEDULE_KEYS,
  SCHEDULE_STEPS,
  seedSchedule,
} from "./scheduleFlowModel.js";
import { priorityWords, ReviewStep, SceneStep, TimeZoneNote, WhenStep } from "./ScheduleSteps.jsx";
import { programState } from "./showState.js";
import { whenWords, windowWords } from "./timeWords.js";
import { useCardMutation } from "./useCardMutation.js";
import { SaveBar } from "./patterns/save-bar.tsx";

const EMPTY = {};

// Each step's heading: the one question it asks.
const HEADINGS = {
  scene: "Which Scene?",
  when: "When should it show?",
  review: "Check your Program",
};

/**
 * The Schedule section (flow design §7 J6, "Schedule it"; Power and the week): the region
 * "Programs", with the Program cards and the Schedule flow, Scene → When → Review.
 *
 * A Program shows one Scene every week on its days, from its start to its end on the house
 * clock (central/runtime.py `Program`), at a priority. Central works out every window in the
 * house's time zone and serves each Program's current and next window
 * (`runtime.program_windows`); the console shows them and works out no times.
 *
 * THE CONTAINER. This component owns the flow's one draft (`useFlowDraft`) and never
 * unmounts (the shell keeps Show sections mounted, rule 2), so a step change, a section
 * change, a poll or the sign-in overlay cannot lose the draft.
 *
 * THE WRITES (central/program_routes.py). Create: one `POST /v1/operator/programs`; Central
 * names the Program. A create Central did not answer may have been stored: the flow stays on
 * Review and says so. Save: `PUT /v1/operator/programs/{id}` with the Program the editor
 * loaded (`expected`) and the draft (`next`); it applies from the next window (the window
 * playing now keeps its end). A 409 "changed" carries the stored Program: Reload reseeds the
 * draft from it. Save and apply now (`POST …/apply-now`, the same body, through the SaveBar on
 * an edit's Review): the window playing now finishes gracefully and the new rule plays at once.
 * Remove: `DELETE …?revision=N`; a playing window finishes gracefully. A
 * refusal code that names a field (no-days, unknown-scene, needs-place) returns to its step.
 *
 * @param {{snapshot: object|null, route: import("./routes.js").Route|null,
 *          navigate: (route: import("./routes.js").Route, options?: {replace?: boolean}) => void,
 *          recentScene: {sceneId: string, seq: number}|null,
 *          markDraft: (section: string, dirty: boolean) => void}} props
 */
export function ProgramsRegion({ snapshot, route, navigate, recentScene, markDraft }) {
  const definitions = snapshot?.runtime?.definitions ?? EMPTY;
  // Stored Programs are the runtime programs map, keyed by program_id
  // (central/operator_snapshot.py `runtime_document`).
  const programsMap = snapshot?.runtime?.programs ?? EMPTY;
  const programs = useMemo(
    () => Object.values(programsMap).sort((a, b) => a.name.localeCompare(b.name) || a.program_id.localeCompare(b.program_id)),
    [programsMap],
  );
  const programIds = useMemo(() => new Set(Object.keys(programsMap)), [programsMap]);
  const removal = useCardMutation();
  // Forget a removed Program's card receipt once a snapshot has shown it absent.
  useEffect(() => removal.retain(programIds), [programIds, removal.feedback, removal.retain]);

  const [current, setCurrent] = useState(null);       // the stored Program a 409 answered with
  const seed = useMemo(
    () => seedSchedule(recentScene?.sceneId ?? null, definitions,
      current === null ? programsMap : { ...programsMap, [current.program_id]: current }),
    [recentScene, definitions, programsMap, current],
  );
  const draft = useFlowDraft(seed);
  const value = draft.value ?? NEW_PROGRAM_DRAFT;
  const editingId = editedId(draft.key);
  const [reloaded, setReloaded] = useState(null);
  const [serverProblem, setServerProblem] = useState(null);

  const regionRef = useRef(/** @type {HTMLElement|null} */ (null));
  const saveRef = useRef(/** @type {HTMLButtonElement|null} */ (null));
  const refs = useFlowRefs();

  // PREFILL: a Scene handed over after the draft opened (a new draft is seeded with it).
  const handOver = useSceneHandOver({
    recentScene,
    draft,
    sceneId: value.sceneId,
    accepts: (sceneId) => definitions[sceneId] !== undefined,
    choose: (sceneId) => draft.patch({ sceneId }),
  });

  const focusRegion = () => regionRef.current?.focus();
  const confirm = useConfirm(focusRegion, (_result, request) =>
    request.after !== undefined ? request.after() : focusRegion(),
  );

  const write = useFlowWrite({ draft, confirm, failure: "Could not save Program" });
  // A refusal Central routed to a field stands until the draft changes.
  const refusedFor = useRef(null);
  useEffect(() => {
    if (refusedFor.current !== null && !sameValue(refusedFor.current, value)) {
      refusedFor.current = null;
      setServerProblem(null);
    }
  }, [value]);
  const problemList = useMemo(
    () =>
      inStepOrder(
        [...programDraftProblems(value), ...(serverProblem === null ? [] : [serverProblem])],
        SCHEDULE_FIELD_STEP,
        SCHEDULE_STEPS,
      ),
    [value, serverProblem],
  );
  const problems = useProblems(problemList);

  const flow = useFlowInstance({
    section: "schedule",
    draft,
    route,
    navigate,
    markDraft,
    keys: SCHEDULE_KEYS,
    refs,
    availability: (key) => {
      const id = editedId(key);
      if (id === null) return "ok";
      if (programsMap[id] === undefined) return "missing";
      return programEditDraft(programsMap[id]) === null ? "unavailable" : "ok";
    },
    steps: SCHEDULE_STEPS,
    fieldStep: SCHEDULE_FIELD_STEP,
    advancedFields: SCHEDULE_ADVANCED_FIELDS,
    problemList,
    problems,
    confirm,
    onOpened: () => { handOver.clear(); setReloaded(null); setCurrent(null); setServerProblem(null); },
  });
  const { step } = flow;

  // A refusal routed to a field opens its step once the write is over (a held draft stays put).
  const [routeTo, setRouteTo] = useState(null);
  useEffect(() => {
    if (routeTo !== null && !write.busy) {
      setRouteTo(null);
      flow.openField(routeTo);
    }
  }, [routeTo, write.busy, flow]);

  /** A refusal: a field's code returns to its step; "changed" offers Reload; else the code. */
  const refused = (sent, result) => {
    const routed = REFUSAL_FIELDS[result.error];
    if (routed !== undefined) {
      refusedFor.current = value;
      setServerProblem(routed);
      setRouteTo(routed.field);      // once the write has let go of the draft
      sent.say(`Not saved: ${routed.message}`);
    } else if (result.error === "changed" && result.data?.current) {
      setCurrent(result.data.current);
      sent.say(`Program ${value.name} changed since you opened it. Reload it to review the saved Program.`);
    } else if (result.error === "missing") {
      sent.say(`Program ${value.name} no longer exists.`);
    } else {
      sent.refused(result);
    }
  };

  const storedProgram = editingId === null ? null : programsMap[editingId];
  const stale = editingId !== null && (
    storedProgram === undefined || !sameValue(storedProgram, value.expected) || current !== null
  );
  // Reload: reseed from the Program Central answered a 409 with (else the snapshot's).
  const reload = () => {
    draft.reseed();
    problems.reset();
    setCurrent(null);
    setReloaded(`Reloaded Program ${value.name} from Central.`);
  };

  // An edit saves from the next window (PUT), or saves and applies now (POST …/apply-now): the
  // window playing now finishes gracefully and the new rule's window covering now plays at once.
  const saveEdit = (applyNow) => {
    if (stale || !flow.checkAll()) return;
    write.send(flow, async (sent) => {
      const path = `/v1/operator/programs/${encodeURIComponent(editingId)}${applyNow ? "/apply-now" : ""}`;
      const result = await sent.request(() => apiWrite(path, {
        method: applyNow ? "POST" : "PUT", body: { expected: value.expected, next: buildProgram(value) },
      }));
      if (result?.ok) {
        sent.say(applyNow
          ? `Saved Program ${result.data.name} and applied it now: the window playing now finishes and the new rule plays.`
          : `Saved Program ${result.data.name}. It applies from its next window; a window playing now keeps its end.`);
        sent.finish();
      } else if (result === null || result.status >= 500) {
        sent.attempted([editingId]);
        sent.say(`Program ${value.name} ${NOT_CONFIRMED} Save again to confirm.`);
      } else refused(sent, result);
    });
  };

  const onSave = () => {
    if (editingId !== null) {
      saveEdit(false);
      return;
    }
    write.send(flow, async (sent) => {
      const result = await sent.request(() => apiWrite("/v1/operator/programs", {
        method: "POST",
        body: buildProgram(value),
      }));
      if (result?.ok) {
        sent.say(`Scheduled Program ${result.data.name}.`);
        handOver.clear();
        sent.finish();
      } else if (result === null || result.status >= 500) {
        sent.say(`Program ${value.name} ${NOT_CONFIRMED} Check the Programs before scheduling it again.`);
      } else refused(sent, result);
    });
  };

  // --- Removing a Program.
  const removeProgram = useCallback(
    (program) => removal.run(
      program.program_id,
      () => apiWrite(
        `/v1/operator/programs/${encodeURIComponent(program.program_id)}?revision=${program.revision}`,
        { method: "DELETE" },
      ),
      (result) => {
        if (result.ok) {
          return { kind: "accepted", message: `Central removed Program ${program.name}. Waiting for the Program list to update.` };
        }
        if (result.status === 0 || result.status >= 500) {
          return {
            kind: "unknown",
            message: `Removal outcome for Program ${program.name} is unknown. Check the current Program before retrying.`,
          };
        }
        return {
          kind: "refused",
          message: `Program ${program.name} was not removed: ${result.error === "changed" ? "it changed since this card was loaded"
            : result.error ?? `HTTP ${result.status}`}. Check the current Program before retrying.`,
        };
      },
    ),
    [removal],
  );

  // Removing a Program whose window is playing finishes its Run (central/runtime.py
  // `remove_program`), so it is confirmed (§9).
  const onRemove = (event, program) => {
    if (programState(snapshot, program.program_id)?.state === "running") {
      confirm.open(event, removeRunningRequest(program));
    } else {
      removeProgram(program);
    }
  };

  // --- Views.
  const stepProps = { value, patch: draft.patch, problems };
  const { advanced } = flow;
  const views = {
    scene: () => (
      <SceneStep
        {...stepProps}
        definitions={definitions}
        offered={handOver.offered}
        onUseOffered={handOver.take}
      />
    ),
    when: () => <WhenStep {...stepProps} />,
    review: () => <>
      {stale && <div className="notice notice--warn" role="status">
        <p>{storedProgram === undefined ? `Program ${value.name} no longer exists.` :
          `Program ${value.name} changed since you opened it.`}</p>
        <p>Reload it to review the saved Program; Save Program waits until you do.</p>
        {storedProgram !== undefined && <button type="button" onClick={reload}>Reload</button>}
      </div>}
      {reloaded !== null && <p role="status">{reloaded}</p>}
      <ReviewStep {...stepProps} advanced={advanced("review")} onChange={flow.openField}
        windows={editingId === null ? null : snapshot?.runtime?.program_windows?.[editingId] ?? {}} />
      {editingId !== null && (
        <SaveBar label="Apply now" message="Save Program applies from the next window; the window playing now keeps its end.">
          <button type="button" disabled={write.busy || stale} onClick={() => saveEdit(true)}>Save and apply now</button>
        </SaveBar>
      )}
    </>,
  };

  return (
    <section
      ref={regionRef}
      tabIndex={-1}
      className="showrunner__region"
      role="region"
      aria-label="Programs"
    >
      <h2 className="showrunner__region-title">Programs</h2>
      <FlowFrame
        flow={flow}
        draft={draft}
        keys={SCHEDULE_KEYS}
        noun="Program"
        sectionLabel="Schedule"
        confirm={confirm}
        problems={problems}
        writeRef={saveRef}
        newLabel="Schedule a Program"
        cards={
          <>
            <p className="field__hint program-flow__intro">
              Each Program shows one Scene every week on its days, from its start to its end.
              Where none plays, the Default Scene does.
            </p>
            <TimeZoneNote />
            {programs.length === 0 ? (
              <p className="program-scheduling__empty">No Programs yet.</p>
            ) : (
              <ProgramCards programs={programs} snapshot={snapshot} onRemove={onRemove}
                onEdit={(id, event) => flow.start(editKey(id), event)}
                removing={removal.pending} removalFeedback={removal.feedback} />
            )}
          </>
        }
        unavailableReason="this Program has no When to edit"
        title={editingId === null ? "New Program" : `Edit Program ${storedProgram?.name ?? editingId}`}
        steps={SCHEDULE_STEPS}
        formLabel={editingId === null ? "Schedule a Program" : `Edit Program ${storedProgram?.name ?? editingId}`}
        heading={HEADINGS[step]}
        busy={write.busy}
        onWrite={onSave}
        writeLabel={editingId !== null ? "Save Program" : "Schedule Program"}
        writeDisabled={write.busy || stale}
        problemsLabel="Program problems"
      >
        {step !== null && views[step]()}
      </FlowFrame>
    </section>
  );
}

// A Program state's chip (SummaryCard): the states that ask for a look.
const STATE_CHIPS = {
  running: { tone: "ok", text: "Playing" },
  refused: { tone: "alarm", text: "Refused" },
  none: { tone: "todo", text: "No window" },
};

/**
 * Stored Programs as summary cards named by the Program's name: the Scene, the When
 * ("Mon–Fri, 17:00–23:00"), the window playing now or the next one (Central's, on the house
 * clock), the priority and the state; Edit and Remove.
 */
function ProgramCards({ programs, snapshot, onRemove, onEdit, removing = new Set(), removalFeedback = {} }) {
  return (
    <ul className="card-grid" role="list">
      {programs.map((program) => {
        const id = program.program_id;
        const state = programState(snapshot, id);
        const windows = snapshot?.runtime?.program_windows?.[id] ?? {};
        const shown = windows.current ?? windows.next ?? null;
        const feedback = Object.hasOwn(removalFeedback, id) ? removalFeedback[id] : null;
        return (
          <li key={id} className="card-grid__item">
            <SummaryCard
              title={`Program ${program.name}`}
              chip={STATE_CHIPS[state.state] ?? null}
              lines={[
                { label: "Scene", value: `Scene ${program.scene_id}` },
                { label: "When", value: whenWords(program.when) },
                {
                  label: windows.current ? "Now" : "Next",
                  value: shown === null ? "None in the coming week" : windowWords(shown),
                },
                { label: "Priority", value: priorityWords(program.priority) },
                {
                  label: "State",
                  value: (
                    <>
                      {state.label}
                      {state.hint !== null && (
                        <span className="program-scheduling__program-hint">{` ${state.hint}`}</span>
                      )}
                    </>
                  ),
                },
                ...(feedback ? [{
                  label: "Removal",
                  value: <span role="status" aria-live="polite">{feedback.message}</span>,
                }] : []),
              ]}
              actions={
                <>
                  {programEditDraft(program) !== null ? (
                    <button type="button" aria-label={`Edit program ${program.name}`} onClick={(event) => onEdit(id, event)}>Edit</button>
                  ) : (
                    <p className="field__hint program-list__edit-hint">
                      Editing unavailable: this Program has no When.
                    </p>
                  )}
                  <button
                    type="button"
                    aria-label={`Remove program ${program.name}`}
                    aria-busy={removing.has(id)}
                    disabled={removing.has(id) || feedback?.kind === "accepted"}
                    onClick={(event) => onRemove(event, program)}
                  >
                    {removing.has(id) ? "Removing…" : "Remove"}
                  </button>
                </>
              }
            />
          </li>
        );
      })}
    </ul>
  );
}

/** Remove a Program whose window is playing: its Run is asked to finish now. */
function removeRunningRequest(program) {
  return {
    key: `remove-program:${program.program_id}`,
    title: `Remove program ${program.name}?`,
    confirmLabel: "Confirm remove",
    body: (
      <p>
        {`It is playing now. Its Run of ${program.scene_id} is asked to finish: it ends at the end ` +
          "of its current cycle, after any outro. No later window plays."}
      </p>
    ),
    run: async () => {
      const result = await apiWrite(
        `/v1/operator/programs/${encodeURIComponent(program.program_id)}?revision=${program.revision}`,
        { method: "DELETE" },
      );
      if (result.ok) {
        return { state: "done", message: `Program ${program.name} removed.` };
      }
      if (result.status >= 500) {
        return { state: "unknown", message: UNKNOWN_MESSAGE };
      }
      return { state: "refused", message: `Not removed: ${result.error ?? result.status}.` };
    },
  };
}
