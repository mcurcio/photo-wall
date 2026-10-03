import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { draftId, planWindows } from "./authoring.js";
import { useConfirm } from "./ConfirmAction.jsx";
import { UNKNOWN_MESSAGE } from "./equipmentApi.js";
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
  programEditDraft,
  programDraftProblems,
  effectiveProgramTimes,
  SCHEDULE_ADVANCED_FIELDS,
  SCHEDULE_FIELD_STEP,
  SCHEDULE_KEYS,
  SCHEDULE_STEPS,
  seedSchedule,
  separateWindows,
} from "./scheduleFlowModel.js";
import { ReviewStep, SceneStep, TimeZoneNote, WhenStep } from "./ScheduleSteps.jsx";
import { isPastProgram, programState } from "./showState.js";
import { windowLabel } from "./timeWords.js";
import { useCardMutation } from "./useCardMutation.js";

const EMPTY = {};

// Each step's heading: the one question it asks.
const HEADINGS = {
  scene: "Which Scene?",
  when: "When should it show?",
  review: "Check your Program",
};

/**
 * The Schedule section (flow design §7 J6, "Schedule it"): the region "Programs", with
 * the Program cards and the Schedule flow, Scene → When → Review.
 *
 * A Program binds a Scene to a SINGLE time window with a priority (design J4; `PUT
 * /v1/operator/programs/{id}`). The optional helper (When's Advanced) creates N
 * SEPARATE windows in one action: N independent Programs `<id>-<n>`, window 1 the
 * entered one and each later one on the next ticked weekday at the same local clock
 * times, each a real single-window `PUT`, managed and removed one by one. Nothing here
 * is a recurring rule: central stores none (design R2/Q2). Times are entered and shown
 * in the browser's time zone, which is named.
 *
 * THE CONTAINER. This component owns the flow's one draft (`useFlowDraft`) and never
 * unmounts (the shell keeps Show sections mounted, rule 2), so a step change, a section
 * change, a poll or the sign-in overlay cannot lose the draft. The step views
 * (ScheduleSteps.jsx) hold no draft state.
 *
 * ROUTES AND STEPS are the flow kit's (flow/useFlowInstance.js): `#/schedule` shows the
 * cards; `#/schedule/new/<step>` a step (scheduleFlowModel.js `SCHEDULE_KEYS`). Continue
 * checks its own step; Review's write checks every value and routes each problem
 * through `SCHEDULE_FIELD_STEP` to its step, opening its Advanced ("Repeat on", "Number
 * of windows", "Priority", "Id").
 *
 * PREFILL. A new draft's Scene is the shell's `recentScene` while it is stored: the Scene just saved, or
 * picked with a Scene card's "Schedule it". A later hand-over refills a draft the
 * operator has not changed; a changed draft is kept, and its Scene step offers the
 * handed-over Scene instead (the kit's `useSceneHandOver`, shared with Show now).
 *
 * THE WRITE (authoring.js owns every rule). One window: ONE `PUT` of `<id>`, "Schedule
 * Program". More: "Add separate windows" sends one `PUT` per window. A partial failure
 * names the ids refused (4xx) and those not confirmed (5xx or no answer: they may be
 * saved), keeps the flow on Review, and sending the same draft again sends only the
 * windows not yet confirmed (a `PUT` of the same body is idempotent), so its own windows
 * never read as collisions. One window Central did not answer "may have been saved" in
 * the same way, and Schedule Program again confirms it (the kit's `attempt`). Writes wrap the shared `useMutate()` (one Plane A refresh
 * after a write); a write that stores everything ends the flow (`finish`).
 *
 * @param {{snapshot: object|null, route: import("./routes.js").Route|null,
 *          navigate: (route: import("./routes.js").Route, options?: {replace?: boolean}) => void,
 *          recentScene: {sceneId: string, seq: number}|null,
 *          markDraft: (section: string, dirty: boolean) => void}} props
 */
export function ProgramsRegion({ snapshot, route, navigate, recentScene, markDraft }) {
  const definitions = snapshot?.runtime?.definitions ?? EMPTY;
  // Stored Programs are the runtime programs map, keyed by program_id
  // (central/app.py -> runtime.export_state()["programs"]).
  const programsMap = snapshot?.runtime?.programs ?? EMPTY;
  const programs = useMemo(() => Object.values(programsMap), [programsMap]);
  const programIds = useMemo(() => new Set(Object.keys(programsMap)), [programsMap]);
  const now = snapshot?.runtime?.current?.now;
  const removal = useCardMutation();
  // A removed Program id may be used again. Forget its old card receipt once a
  // snapshot has shown it absent, so a newly scheduled Program starts clean.
  useEffect(() => removal.retain(programIds), [programIds, removal.feedback, removal.retain]);

  const draft = useFlowDraft(seedSchedule(recentScene?.sceneId ?? null, definitions, programsMap));
  const value = draft.value ?? NEW_PROGRAM_DRAFT;
  const editingId = editedId(draft.key);
  const [reloaded, setReloaded] = useState(null);
  const [serverConflict, setServerConflict] = useState(null);

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

  // One confirmation (ConfirmAction) for this section: removing a running or due
  // Program, and discarding the draft. A request's `after` runs once it is done; after a
  // removal, or when its opener is gone, the region takes focus.
  const focusRegion = () => regionRef.current?.focus();
  const confirm = useConfirm(focusRegion, (_result, request) =>
    request.after !== undefined ? request.after() : focusRegion(),
  );

  // A write that did not confirm every id it sent (flow/useFlowWrite.js NOT CONFIRMED).
  const write = useFlowWrite({ draft, confirm, failure: "Could not schedule Program" });
  const pending = write.attempt;
  const problemList = useMemo(
    () =>
      inStepOrder(
        programDraftProblems(value, programIds, now, pending?.ids, editingId !== null),
        SCHEDULE_FIELD_STEP,
        SCHEDULE_STEPS,
      ),
    [value, programIds, now, pending, editingId],
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
      if (programEditDraft(programsMap[id]) === null || programState(snapshot, id)?.state !== "upcoming") return "unavailable";
      return "ok";
    },
    steps: SCHEDULE_STEPS,
    fieldStep: SCHEDULE_FIELD_STEP,
    advancedFields: SCHEDULE_ADVANCED_FIELDS,
    problemList,
    problems,
    confirm,
    onOpened: () => { handOver.clear(); setReloaded(null); setServerConflict(null); },
  });
  const { step } = flow;

  // --- The write.
  const put = (programId, startsAt, endsAt) =>
    apiWrite(`/v1/operator/programs/${encodeURIComponent(programId)}`, {
      method: "PUT",
      body: buildProgram({
        programId,
        sceneId: value.sceneId,
        startsAt,
        endsAt,
        priority: Number(value.priority),
      }),
    });

  /** Everything is stored: say so on the cards and end the flow. */
  const stored = (sent, message) => {
    sent.say(message);
    handOver.clear();
    sent.finish();
  };

  const scheduleOne = async (sent, id) => {
    const times = effectiveProgramTimes(value);
    const result = await sent.request(() => put(id, times.startsAt, times.endsAt));
    if (result === null || result.status >= 500) {
      // It may have been stored: scheduling the same draft again confirms it.
      sent.attempted([id]);
      sent.say(`Program ${id} ${NOT_CONFIRMED} Schedule Program again to confirm.`);
    } else if (result.ok) {
      stored(sent, `Scheduled Program ${id}.`);
    } else {
      sent.refused(result);
    }
  };

  const addSeparateWindows = async (sent, id) => {
    const count = Number(value.count);
    const planned = planWindows({ ...value, count }).map((window, index) => ({
      ...window,
      programId: `${id}-${index + 1}`,
    }));
    const confirmed = new Set(pending?.confirmed ?? []);
    const sending = planned.filter((window) => !confirmed.has(window.programId));
    // A write that did not complete leaves every window unconfirmed.
    const results = (await sent.request(() =>
      Promise.all(
        sending.map(({ programId, startsAt, endsAt }) =>
          put(programId, startsAt, endsAt).then(
            (result) => (result.ok ? "created" : result.status >= 500 ? "unknown" : "refused"),
            () => "unknown",
          ),
        ),
      ),
    )) ?? sending.map(() => "unknown");
    const outcome = (kind) =>
      sending.filter((_window, index) => results[index] === kind).map((window) => window.programId);
    outcome("created").forEach((programId) => confirmed.add(programId));
    if (confirmed.size === planned.length) {
      stored(sent, `Created ${planned.length} separate Programs.`);
      return;
    }
    const [refused, unknown] = [outcome("refused"), outcome("unknown")];
    sent.attempted(
      planned.map((window) => window.programId),
      [...confirmed],
    );
    sent.say(
      [
        `Created ${confirmed.size} of ${planned.length} separate Programs.`,
        refused.length > 0 ? `Not created: ${refused.join(", ")}.` : null,
        unknown.length > 0 ? `Not confirmed: ${unknown.join(", ")}; Central did not answer.` : null,
        "Add separate windows again to send only these.",
      ]
        .filter((part) => part !== null)
        .join(" "),
    );
  };

  const storedProgram = editingId === null ? null : programsMap[editingId];
  const editState = editingId === null ? null : programState(snapshot, editingId)?.state;
  const stale = editingId !== null && (
    storedProgram === undefined || !sameValue(storedProgram, value.expected) || editState !== "upcoming" || serverConflict !== null
  );
  const reload = () => {
    draft.reseed();
    problems.reset();
    setServerConflict(null);
    setReloaded(`Reloaded Program ${editingId} from the current snapshot.`);
  };
  const onSave = () => {
    if (editingId !== null) {
      if (stale || !flow.checkAll()) return;
      write.send(flow, async (sent) => {
        const times = effectiveProgramTimes(value);
        const program = buildProgram({ programId: editingId, sceneId: value.sceneId,
          startsAt: times.startsAt, endsAt: times.endsAt, priority: Number(value.priority) });
        const result = await sent.request(() => apiWrite(
          `/v1/operator/programs/${encodeURIComponent(editingId)}/replace`,
          { method: "POST", body: { expected: value.expected, program } },
        ));
        if (result?.ok) {
          sent.say(`Replaced Program ${editingId}. Changes apply to future Runs; an active Run keeps its secured assignment.`);
          sent.finish();
        } else if (["program_changed", "program_missing", "program_started", "program_window_started"].includes(result?.error)) {
          setServerConflict(result.error);
          sent.say(`Program ${editingId} changed or started before it could be replaced. Reload the stored Program before editing again.`);
        } else if (result === null || result.status >= 500) {
          sent.attempted([editingId]);
          sent.say(`Program ${editingId} may have been replaced: Central did not answer. Save again to confirm.`);
        } else sent.refused(result);
      });
      return;
    }
    write.send(flow, (sent) =>
      (separateWindows(value) ? addSeparateWindows : scheduleOne)(sent, draftId(value)),
    );
  };

  // --- Removing a Program.
  const removeProgram = useCallback(
    (id) => removal.run(
      id,
      () => apiWrite(`/v1/operator/programs/${encodeURIComponent(id)}`, { method: "DELETE" }),
      (result) => {
        if (result.ok) {
          return { kind: "accepted", message: `Central accepted removal of Program ${id}. Waiting for the Program list to update.` };
        }
        if (result.status === 0 || result.status >= 500) {
          return {
            kind: "unknown",
            message: `Removal outcome for Program ${id} is unknown. Check the current Program state before retrying.`,
          };
        }
        return {
          kind: "refused",
          message: `Program ${id} was not removed: ${result.error?.replaceAll("_", " ") ?? `HTTP ${result.status}`}. Check the current Program state before retrying.`,
        };
      },
    ),
    [removal],
  );

  // Removing a RUNNING Program ends its Run, and removing a DUE one starts
  // its Run and asks it to finish (central/runtime.py `remove_program`), so
  // both are confirmed (§9).
  const onRemove = (event, program) => {
    const state = programState(snapshot, program.program_id)?.state;
    if (state === "running" || state === "due") {
      confirm.open(event, removeRunningRequest(program, state));
    } else {
      removeProgram(program.program_id);
    }
  };

  // "Past": the window has ended and the Program is neither running nor due.
  const past = programs.filter((program) => isPastProgram(snapshot, program));
  const current = programs.filter((program) => !isPastProgram(snapshot, program));

  // --- Views.
  const stepProps = { value, patch: draft.patch, problems, editing: editingId !== null };
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
    when: () => <WhenStep {...stepProps} advanced={advanced("when")} editableWindows={editingId === null} />,
    review: () => <>
      {stale && <div className="notice notice--warn" role="status">
        <p>{storedProgram === undefined ? `Program ${editingId} no longer exists.` :
          editState !== "upcoming" ? `Program ${editingId} is no longer upcoming; it cannot be edited.` :
            serverConflict !== null ? `Central refused the replacement (${serverConflict.replaceAll("_", " ")}).` :
              `Program ${editingId} changed since you opened it.`}</p>
        <p>{editState !== "upcoming" ? "Changes cannot alter a Program after its window starts." :
          "Reload it to review the latest saved Program; Replace Program waits until you do."}</p>
        {storedProgram !== undefined && editState === "upcoming" && <button type="button" onClick={reload}>Reload</button>}
        {editState !== "upcoming" && <button type="button" onClick={flow.leave}>Return to Programs</button>}
      </div>}
      {reloaded !== null && <p role="status">{reloaded}</p>}
      <ReviewStep {...stepProps} advanced={advanced("review")} onChange={flow.openField}
        editableWindows={editingId === null} editing={editingId !== null} />
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
              Each Program shows one Scene during one window. Several separate windows —
              individual Programs, each stored and removed on its own — can be added at once
              under Advanced on the When step.
            </p>
            <TimeZoneNote />
            {programs.length === 0 ? (
              <p className="program-scheduling__empty">No Programs yet.</p>
            ) : (
              <>
                {/* Every stored Program by its program_id, each one discrete window with
                    its display state (showState.js); past windows sit under a closed
                    "Past (N)" disclosure. */}
                <ProgramCards programs={current} snapshot={snapshot} onRemove={onRemove} onEdit={(id, event) => flow.start(editKey(id), event)} removing={removal.pending} removalFeedback={removal.feedback} />
                {past.length > 0 && (
                  <details className="program-cards__past">
                    <summary>{`Past (${past.length})`}</summary>
                    <ProgramCards programs={past} snapshot={snapshot} onRemove={onRemove} onEdit={(id, event) => flow.start(editKey(id), event)} removing={removal.pending} removalFeedback={removal.feedback} />
                  </details>
                )}
              </>
            )}
          </>
        }
        unavailableReason="only upcoming Programs with supported stored times can be edited"
        title={editingId === null ? "New Program" : `Edit Program ${editingId}`}
        steps={SCHEDULE_STEPS}
        formLabel={editingId === null ? "Schedule a Program" : `Edit Program ${editingId}`}
        heading={HEADINGS[step]}
        busy={write.busy}
        onWrite={onSave}
        writeLabel={editingId !== null ? "Replace Program" : separateWindows(value) ? "Add separate windows" : "Schedule Program"}
        writeDisabled={write.busy || stale}
        problemsLabel="Program problems"
      >
        {step !== null && views[step]()}
      </FlowFrame>
    </section>
  );
}

// A Program state's chip (SummaryCard): the states that ask for a look, each with its
// word; the others read on the State line alone.
const STATE_CHIPS = {
  running: { tone: "ok", text: "Running" },
  refused: { tone: "alarm", text: "Refused" },
  missed: { tone: "todo", text: "Missed" },
};

/**
 * Stored Programs as summary cards named `Program X`: the Scene, the window in local
 * time, the priority, and the display state with its hint (slice 3 §9); Remove.
 */
function ProgramCards({ programs, snapshot, onRemove, onEdit, removing = new Set(), removalFeedback = {} }) {
  return (
    <ul className="card-grid" role="list">
      {programs.map((program) => {
        const id = program.program_id;
        const state = programState(snapshot, id);
        const editableTime = programEditDraft(program) !== null;
        const feedback = Object.hasOwn(removalFeedback, id) ? removalFeedback[id] : null;
        return (
          <li key={id} className="card-grid__item">
            <SummaryCard
              title={`Program ${id}`}
              chip={STATE_CHIPS[state.state] ?? null}
              lines={[
                { label: "Scene", value: `Scene ${program.scene_id}` },
                { label: "Window", value: windowLabel(program) },
                { label: "Priority", value: `Priority ${program.priority}` },
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
                  {state.state === "upcoming" && editableTime ? (
                    <button type="button" aria-label={`Edit program ${id}`} onClick={(event) => onEdit(id, event)}>Edit</button>
                  ) : (
                    <p className="field__hint program-list__edit-hint">
                      {state.state === "running" ? "Editing unavailable: this Program has an active Run; changes cannot alter it." :
                        state.state === "due" ? "Editing unavailable: its window has started." :
                        !editableTime ? "Editing unavailable: its times are outside this editor's supported range." :
                          state.state === "old" ? "Editing unavailable: its outcome details have expired." :
                            state.state === "missed" || state.state === "ran" ? "Editing unavailable: this Program has already ended." :
                              "Editing is available before the Program window starts."}
                    </p>
                  )}
                  <button
                    type="button"
                    aria-label={`Remove program ${id}`}
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

/**
 * Remove a running or due Program: its Run — for a due one, started first — is
 * asked to finish now (central/runtime.py `remove_program`).
 */
function removeRunningRequest(program, state) {
  const lead =
    state === "due"
      ? `Its window has started, so Central starts its Run of ${program.scene_id} and asks it to finish`
      : `It is running now. Its Run of ${program.scene_id} is asked to finish`;
  return {
    key: `remove-program:${program.program_id}`,
    title: `Remove program ${program.program_id}?`,
    confirmLabel: "Confirm remove",
    body: (
      <p>
        {`${lead}: it ends at the end of its current cycle, after any outro. Later windows are ` +
          "separate Programs and stay."}
      </p>
    ),
    run: async () => {
      const result = await apiWrite(`/v1/operator/programs/${encodeURIComponent(program.program_id)}`, {
        method: "DELETE",
      });
      if (result.ok) {
        return { state: "done", message: `Program ${program.program_id} removed.` };
      }
      if (result.status >= 500) {
        return { state: "unknown", message: UNKNOWN_MESSAGE };
      }
      return { state: "refused", message: `Not removed: ${result.error ?? result.status}.` };
    },
  };
}
