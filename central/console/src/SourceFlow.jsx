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
import { editedId, editKey } from "./flow/instance.js";
import { useFlowWrite } from "./flow/useFlowWrite.js";
import { codeWords, sourceFilters } from "./mediaHealth.js";
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
import { namedSource, sourceName } from "./sourceNames.js";
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
 * SAVE writes one `PUT /v1/operator/source-names/{name}` with the draft's expected
 * revision. Central owns the hidden immutable revision and rewrites stored Scenes to
 * that ref; the console uses its returned ref for an inline Scene hand-off. Delete is
 * confirmed and guarded by Central when Scenes still use the Source.
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
  const editingName = editedId(draft.key);
  const stored = editingName === null ? null : namedSource(sources, editingName);
  const stale = editingName !== null && stored !== undefined && stored !== null &&
    draft.baseRevision !== null && stored.revision !== draft.baseRevision;
  const handOff = useHandOffTo(handOffs, "sources");
  const mutate = useMutate();

  const refs = useFlowRefs();

  const problemList = useMemo(
    () => inStepOrder(sourceProblems(value), SOURCE_FIELD_STEP, SOURCE_STEPS),
    [value, editingName],
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
    availability: (key) => editedId(key) !== null && !namedSource(sources, editedId(key)) ? "missing" : "ok",
    steps: SOURCE_STEPS,
    fieldStep: SOURCE_FIELD_STEP,
    advancedFields: sourceAdvancedFields(rule),
    problemList,
    problems,
    confirm,
    handOff,
  });
  const { step } = flow;
  const write = useFlowWrite({ draft, confirm, failure: "Could not save Source" });

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
    write.send(flow, async (sent) => {
      if (stale) {
        sent.say("This Source changed since you opened it. Reload it before saving.");
        return;
      }
      const name = editingName ?? value.sourceName.trim();
      const result = await sent.request(() =>
        apiWrite(`/v1/operator/source-names/${encodeURIComponent(name)}`, {
          method: "PUT",
          body: buildSourceSpec({
            ...value,
            expectedRevision: editingName === null ? null : draft.baseRevision,
            newName: editingName !== null && value.sourceName.trim() !== editingName
              ? value.sourceName.trim() : null,
            originalCapturedFrom: editingName !== null && value.capturedFrom === draft.seeded?.capturedFrom
              ? stored?.spec?.captured_from ?? null : undefined,
            originalCapturedUntil: editingName !== null && value.capturedUntil === draft.seeded?.capturedUntil
              ? stored?.spec?.captured_until ?? null : undefined,
            connectionRef: value.connectionRef.trim(),
          }),
        }),
      );
      if (result === null) {
        sent.incomplete();
      } else if (result.ok) {
        sent.say(`Saved Source ${result.data?.name ?? name}.`);
        sent.finish(undefined, { sourceRef: result.data?.source_ref });
      } else if (result.error === "source_revision_conflict") {
        sent.say("This Source changed since you opened it. Reload it and review the latest saved Source.");
      } else if (result.error === "source_name_exists") {
        sent.say(`A Source named ${value.sourceName.trim()} already exists. Choose another name or edit that Source.`);
      } else {
        sent.refused(result);
      }
    });

  const deleteSource = (source, event) => {
    const name = sourceName(source);
    const revision = source.revision;
    confirm.open(event, {
      key: `delete-source:${name}`,
      title: `Delete Source ${name}?`,
      confirmLabel: "Confirm delete",
      body: (
        <>
          <p>{`This removes Source ${name} from new selections. It does not delete photos from Immich.`}</p>
          <p>If a Scene uses this Source, choose a different Source for that Scene before deleting.</p>
        </>
      ),
      run: async () => {
        const result = await apiWrite(
          `/v1/operator/source-names/${encodeURIComponent(name)}?expected_revision=${revision}`,
          { method: "DELETE" },
        );
        if (result.ok) return { state: "done", message: `Source ${name} deleted.` };
        if (result.error === "source_in_use") {
          const scenes = result.data?.scene_ids;
          return { state: "refused", message: Array.isArray(scenes) && scenes.length > 0
            ? `Used by Scenes ${scenes.join(", ")}. Edit them to choose another Source first.`
            : "This Source is used by a Scene. Edit the Scene to choose another Source first." };
        }
        if (result.error === "source_revision_conflict") return { state: "changed", message: "This Source changed. Close this dialog and review its latest saved details." };
        return { state: "refused", message: `Could not delete Source ${name}: ${result.error ?? result.status}.` };
      },
    });
  };

  const stepProps = { value, patch: draft.patch, problems };
  const views = {
    include: () => <IncludeStep {...stepProps} />,
    name: () => (
      <NameStep
        {...stepProps}
        rule={rule}
        advanced={flow.advanced("name")}
        editing={editingName !== null}
      />
    ),
    review: () => (
      <>
        {stale && (
          <div className="notice notice--warn" role="status">
            <p>This Source changed since you opened it. Reload to review its latest saved details.</p>
            <button type="button" onClick={() => draft.reseed()}>Reload</button>
          </div>
        )}
        {editingName !== null && (
          <p>Changes to this Source apply to future Runs. Runs already started keep their saved selection.</p>
        )}
        <SourceReview value={value} onChange={flow.openField} />
      </>
    ),
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
      cards={<SourceCards sources={sources} onRefresh={refreshSource} onEdit={(name, event) => flow.start(editKey(name), event)} onDelete={deleteSource} busy={write.busy} />}
      title={editingName === null ? "New photo source" : `Edit Source ${editingName}`}
      steps={SOURCE_STEPS}
      formLabel="Configure a Source"
      heading={HEADINGS[step]}
      busy={write.busy}
      onWrite={onSave}
      writeLabel={editingName === null ? "Save source" : "Save changes"}
      writeDisabled={write.busy || stale}
      problemsLabel="Source problems"
    >
      {step !== null && views[step]()}
    </FlowFrame>
  );
}

/**
 * The saved Sources as summary cards: each by its plain logical name, with its status, when it
 * last succeeded (or that no refresh has succeeded), what it includes
 * and its connection, and Refresh ("Refresh <ref>").
 *
 * @param {{sources: ReadonlyArray<object>, onRefresh: (sourceRef: string) => void}} props
 */
function SourceCards({ sources, onRefresh, onEdit, onDelete, busy }) {
  if (sources.length === 0) {
    return <p className="showrunner__empty">No Sources yet.</p>;
  }
  return (
    <ul className="card-grid" role="list">
      {sources.map((source) => (
        <li key={source.source_ref} className="card-grid__item">
          <SummaryCard
            title={sourceName(source)}
            lines={[
              { label: "Status", value: source.status },
              {
                label: "Refreshed",
                value: source.last_success
                  ? `Last refreshed ${new Date(source.last_success * 1000).toLocaleString()}`
                  : source.refresh_completed_revision > 0
                    ? "No successful refresh"
                    : "Awaiting refresh",
              },
              ...(source.status !== "ok" && source.diagnostics?.length
                ? [{ label: "Issue", value: sourceIssue(source.diagnostics) }]
                : []),
              { label: "Includes", value: includesWords(source.spec) },
              { label: "Connection", value: source.spec?.connection_ref ?? "" },
            ]}
            actions={
              <>
                <button type="button" aria-label={`Refresh ${sourceName(source)}`} onClick={() => onRefresh(source.source_ref)}>
                  Refresh
                </button>
                <button type="button" aria-label={`Edit Source ${sourceName(source)}`} disabled={busy} onClick={(event) => onEdit(sourceName(source), event)}>
                  Edit
                </button>
                <button type="button" aria-label={`Delete Source ${sourceName(source)}`} disabled={busy} onClick={(event) => onDelete(source, event)}>
                  Delete
                </button>
              </>
            }
          />
        </li>
      ))}
    </ul>
  );
}

const SOURCE_ISSUES = {
  unsupported_version: "This Photo Wall release does not support the Immich version.",
  upstream_permission: "Immich denied access. Check the API key permissions.",
  owner_mismatch: "The Immich key belongs to a different user.",
  connection_unknown: "Connection is not configured in the media worker.",
};

function sourceIssue(diagnostics) {
  return [...new Set(diagnostics.map((entry) => entry.code))]
    .slice(0, 3)
    .map((code) => SOURCE_ISSUES[code] ?? codeWords(code))
    .join(" · ");
}

/** What a stored spec includes, in words: its filters, or everything. */
function includesWords(spec) {
  const filters = sourceFilters(spec);
  return filters.length === 0 ? "All photos and videos" : filters.join(" · ");
}
