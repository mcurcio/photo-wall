import React, { useCallback, useMemo } from "react";

import { apiWrite } from "./apiWrite.js";
import { sourceProblems } from "./authoring.js";
import { useConfirm } from "./ConfirmAction.jsx";
import { useProblems } from "./Field.jsx";
import { FlowFrame } from "./flow/FlowFrame.jsx";
import { inStepOrder } from "./flow/steps.js";
import { SummaryCard } from "./flow/SummaryCard.jsx";
import { useFlowDraft } from "./flow/useFlowDraft.js";
import { useHandOffTo } from "./flow/useHandOff.js";
import { useFlowInstance, useFlowRefs } from "./flow/useFlowInstance.js";
import { useFlowWrite } from "./flow/useFlowWrite.js";
import { sourceFilters } from "./mediaHealth.js";
import {
  buildSourceSpec,
  connectionRule,
  NEW_SOURCE_DRAFT,
  seedSource,
  sourceAdvancedFields,
  SOURCE_FIELD_STEP,
  SOURCE_KEYS,
  SOURCE_STEPS,
} from "./sourceFlowModel.js";
import { IncludeStep, NameStep, SourceReview } from "./SourceSteps.jsx";
import { useMutate } from "./useMutate.js";

const EMPTY = [];

// Each step's heading: the one question it asks.
const HEADINGS = {
  include: "What to include",
  name: "Name this photo source",
  review: "Check your photo source",
};

/**
 * The Photo sources section's content (flow design §7 J5, "Add a photo source"): the
 * Source cards, each with Refresh, and the Source flow, What to include → Name →
 * Review.
 *
 * THE CONTAINER owns the flow's one draft (`useFlowDraft`) and never unmounts (the
 * shell keeps Show sections mounted, rule 2), so a step change, a section change, a
 * poll or the sign-in overlay cannot lose the draft. The step views (SourceSteps.jsx)
 * hold no draft state.
 *
 * ROUTES AND STEPS are the flow kit's (flow/useFlowInstance.js): `#/sources` shows the
 * cards and "New source"; `#/sources/new/<step>` shows a step (sourceFlowModel.js
 * `SOURCE_KEYS`). Continue checks its own step; Save checks them all and routes each
 * problem through `SOURCE_FIELD_STEP` (opening Advanced for the connection when it
 * sits there).
 *
 * THE CONNECTION RULE (sourceFlowModel.js `connectionRule`) is read from the served
 * Sources on every render, so the Name step follows the library as it is; the seed
 * prefills the one-value case.
 *
 * INLINE (flow/handOff.js). When the Scene flow hands off to "sources", the flow opens
 * its new instance, says whom it is for, and returns there: after Save with
 * `{sourceRef}`, on Back from its first step or "Discard and return" with nothing.
 *
 * SAVE writes ONE `PUT /v1/operator/sources/{ref}` (the ref is `name:rev` and may hold
 * a colon, so it is path-encoded) with the stored `SourceSpec` body through the kit's
 * write (flow/useFlowWrite.js: one Plane A refresh, so the new card, or the Scene's
 * picker, lists it), says "Saved Source <ref>." and ends the flow (`finish`).
 *
 * @param {{snapshot: object|null, route: import("./routes.js").Route|null,
 *          navigate: (route: import("./routes.js").Route, options?: {replace?: boolean}) => void,
 *          markDraft: (section: string, dirty: boolean) => void,
 *          handOffs: ReturnType<typeof import("./flow/useHandOff.js").useHandOff>}} props
 */
export function SourceFlow({ snapshot, route, navigate, markDraft, handOffs }) {
  const sources = snapshot?.media?.sources ?? EMPTY;
  const rule = useMemo(() => connectionRule(sources), [sources]);
  const draft = useFlowDraft(seedSource(sources));
  const value = draft.value ?? NEW_SOURCE_DRAFT;
  const handOff = useHandOffTo(handOffs, "sources");
  const mutate = useMutate();

  const refs = useFlowRefs();

  const problemList = useMemo(
    () => inStepOrder(sourceProblems(value), SOURCE_FIELD_STEP, SOURCE_STEPS),
    [value],
  );
  const problems = useProblems(problemList);

  // One confirmation for this section (discarding a draft); its `after` runs once done.
  const confirm = useConfirm(
    () => refs.newRef.current?.focus(),
    (_result, request) => request.after?.(),
  );

  const flow = useFlowInstance({
    section: "sources",
    draft,
    route,
    navigate,
    markDraft,
    keys: SOURCE_KEYS,
    refs,
    steps: SOURCE_STEPS,
    fieldStep: SOURCE_FIELD_STEP,
    advancedFields: sourceAdvancedFields(rule),
    problemList,
    problems,
    confirm,
    handOff,
  });
  const { step } = flow;
  const write = useFlowWrite({ flow, confirm, failure: "Could not save Source" });

  // Refresh re-runs a saved query (POST …/sources/{ref}/refresh) inside useMutate(), so
  // the cards refresh exactly once after the write.
  const refreshSource = useCallback(
    (sourceRef) =>
      mutate(() =>
        apiWrite(`/v1/operator/sources/${encodeURIComponent(sourceRef)}/refresh`, { method: "POST" }),
      ),
    [mutate],
  );

  const onSave = () =>
    write.send(async (sent) => {
      const ref = value.sourceRef.trim();
      const result = await sent.request(() =>
        apiWrite(`/v1/operator/sources/${encodeURIComponent(ref)}`, {
          method: "PUT",
          body: buildSourceSpec({ ...value, sourceRef: ref, connectionRef: value.connectionRef.trim() }),
        }),
      );
      if (result === null) {
        sent.incomplete();
      } else if (result.ok) {
        sent.say(`Saved Source ${ref}.`);
        sent.finish(undefined, { sourceRef: ref });
      } else {
        sent.refused(result);
      }
    });

  const stepProps = { value, patch: draft.patch, problems };
  const views = {
    include: () => <IncludeStep {...stepProps} />,
    name: () => (
      <NameStep
        {...stepProps}
        rule={rule}
        advanced={flow.advanced("name")}
      />
    ),
    review: () => <SourceReview value={value} onChange={flow.openField} />,
  };

  return (
    <FlowFrame
      flow={flow}
      draft={draft}
      keys={SOURCE_KEYS}
      noun="photo source"
      sectionLabel="Photo sources"
      confirm={confirm}
      problems={problems}
      handOff={handOff}
      newLabel="New source"
      cards={<SourceCards sources={sources} onRefresh={refreshSource} />}
      title="New photo source"
      steps={SOURCE_STEPS}
      formLabel="Configure a Source"
      heading={HEADINGS[step]}
      busy={write.busy}
      onWrite={onSave}
      writeLabel="Save source"
      writeDisabled={write.busy}
      problemsLabel="Source problems"
    >
      {step !== null && views[step]()}
    </FlowFrame>
  );
}

/**
 * The saved Sources as summary cards: each by its `name:rev`, with its status, when it
 * was last refreshed ("Awaiting refresh" until its query first runs), what it includes
 * and its connection, and Refresh ("Refresh <ref>").
 *
 * @param {{sources: ReadonlyArray<object>, onRefresh: (sourceRef: string) => void}} props
 */
function SourceCards({ sources, onRefresh }) {
  if (sources.length === 0) {
    return <p className="showrunner__empty">No Sources yet.</p>;
  }
  return (
    <ul className="card-grid" role="list">
      {sources.map((source) => (
        <li key={source.source_ref} className="card-grid__item">
          <SummaryCard
            title={source.source_ref}
            lines={[
              { label: "Status", value: source.status },
              {
                label: "Refreshed",
                value: source.last_success
                  ? `Last refreshed ${new Date(source.last_success * 1000).toLocaleString()}`
                  : "Awaiting refresh",
              },
              { label: "Includes", value: includesWords(source.spec) },
              { label: "Connection", value: source.spec?.connection_ref ?? "" },
            ]}
            actions={
              <button
                type="button"
                aria-label={`Refresh ${source.source_ref}`}
                onClick={() => onRefresh(source.source_ref)}
              >
                Refresh
              </button>
            }
          />
        </li>
      ))}
    </ul>
  );
}

/** What a stored spec includes, in words: its filters, or everything. */
function includesWords(spec) {
  const filters = sourceFilters(spec);
  return filters.length === 0 ? "All photos and videos" : filters.join(" · ");
}
