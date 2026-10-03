import React, { useCallback, useEffect, useMemo, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { sourceProblems } from "./authoring.js";
import { useConfirm } from "./ConfirmAction.jsx";
import { factText } from "./facts.js";
import { useProblems } from "./Field.jsx";
import { FlowFrame } from "./flow/FlowFrame.jsx";
import { inStepOrder } from "./flow/steps.js";
import { SummaryCard } from "./flow/SummaryCard.jsx";
import { useFlowDraft } from "./flow/useFlowDraft.js";
import { useHandOffTo } from "./flow/useHandOff.js";
import { useFlowInstance, useFlowRefs } from "./flow/useFlowInstance.js";
import { editedId, editKey } from "./flow/instance.js";
import { useFlowWrite } from "./flow/useFlowWrite.js";
import { learnPaths, readTagsById } from "./libraryTags.js";
import { codeWords, mediaNow, sourceState } from "./mediaHealth.js";
import {
  buildSourceSpec,
  connectionAnnounced,
  connectionRule,
  NEW_SOURCE_DRAFT,
  seedSource,
  selectionWords,
  sourceAdvancedFields,
  sourceFieldStep,
  SOURCE_HEADINGS,
  sourceKeys,
  sourceSteps,
  unannouncedWords,
} from "./sourceFlowModel.js";
import { refreshFact, refusalIssue, TAG_GONE } from "./sourceWords.js";
import { LibraryStep, NameStep, NarrowStep, SourceReview, TagsStep } from "./SourceSteps.jsx";
import { namedSource, sourceName } from "./sourceNames.js";
import { sourceRefreshPath } from "./mediaApi.js";
import { useCardMutation } from "./useCardMutation.js";
import { usePreview } from "./usePreview.js";

const EMPTY = [];
const PREVIEWED = new Set(["tags", "narrow", "review"]);

/**
 * The Sources section's content (console DDD §37, pass 5): the Source cards, each with
 * Refresh, Edit and Delete, and the Source flow, one question per step: Which library
 * connection? → Choose tags → Narrow it down → Name this Source → Check your Source.
 *
 * THE CONTAINER owns the flow's one draft (`useFlowDraft`), the preview (usePreview.js)
 * and the tag paths it has learnt, and never unmounts (the shell keeps Show sections
 * mounted, rule 2), so a step change, a section change, a poll or the sign-in overlay
 * cannot lose them. The step views (SourceSteps.jsx) hold no draft state.
 *
 * ROUTES AND STEPS are the flow kit's (flow/useFlowInstance.js): `#/sources` shows the
 * cards and "New Source"; `#/sources/new/<step>` shows a step. The connection rule
 * (sourceFlowModel.js `connectionRule`) runs on every render over the worker's reported
 * connections: with exactly one, the connection step is skipped and a new Source opens
 * on Choose tags (`sourceSteps`, `sourceKeys`). Continue checks its own step; Save checks
 * them all and routes each problem to its step (`sourceFieldStep`).
 *
 * THE PREVIEW is what the draft's criteria select, re-asked on each change (R23) while
 * the Tags, Narrow or Review step shows; it is never draft state (asking never dirties
 * a draft) and needs an announced connection: a typed or unreported name gets §37's
 * sentence instead.
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
  const connectionIds = snapshot?.media?.health?.connection_ids ?? null;
  const draft = useFlowDraft(seedSource(sources, connectionIds));
  const value = draft.value ?? NEW_SOURCE_DRAFT;
  const rule = useMemo(
    () => connectionRule(connectionIds, sources, value.connectionRef),
    [connectionIds, sources, value.connectionRef],
  );
  // The flow's shape follows the rule, except for an edit whose saved connection is no
  // longer reported: choosing the one that is must not take its step away mid-answer.
  const seededRule = useMemo(
    () => connectionRule(connectionIds, sources, draft.seeded?.connectionRef ?? ""),
    [connectionIds, sources, draft.seeded],
  );
  const layout = seededRule.selectedUnavailable ? seededRule : rule;
  const steps = sourceSteps(layout);
  const fieldStep = sourceFieldStep(layout);
  const keys = sourceKeys(layout);
  const now = mediaNow(snapshot);
  const sourceRefresh = useCardMutation();
  const editingName = editedId(draft.key);
  const stored = editingName === null ? null : namedSource(sources, editingName);
  const stale = editingName !== null && stored !== undefined && stored !== null &&
    draft.baseRevision !== null && stored.revision !== draft.baseRevision;
  const handOff = useHandOffTo(handOffs, "sources");

  const refs = useFlowRefs();

  const problemList = useMemo(() => {
    const found = sourceProblems(value);
    if (rule.shown === "blocked") {
      found.push({ field: "connection", message: "Configure a connection in the media worker before saving a Source." });
    } else if (rule.selectedUnavailable) {
      found.push({ field: "connection", message: "Choose a connection currently configured in the media worker." });
    }
    return inStepOrder(found, fieldStep, steps);
  }, [value, rule, fieldStep, steps]);
  const problems = useProblems(problemList);

  // The draft's stored body: its preview asks exactly what Save would store.
  const spec = useMemo(() => buildSourceSpec({
    ...value,
    expectedRevision: editingName === null ? null : draft.baseRevision,
    newName: editingName !== null && value.sourceName.trim() !== editingName
      ? value.sourceName.trim() : null,
    originalCapturedFrom: editingName !== null && value.capturedFrom === draft.seeded?.capturedFrom
      ? stored?.spec?.captured_from ?? null : undefined,
    originalCapturedUntil: editingName !== null && value.capturedUntil === draft.seeded?.capturedUntil
      ? stored?.spec?.captured_until ?? null : undefined,
    connectionRef: value.connectionRef.trim(),
  }), [value, editingName, draft.baseRevision, draft.seeded, stored]);
  const previewPayload = useMemo(() => {
    const { expected_revision: _revision, new_name: _newName, ...payload } = spec;
    return payload;
  }, [spec]);

  const announced = connectionAnnounced(rule, value.connectionRef);
  const invalidWindow = problemList.some(({ field }) => field === "from" || field === "until");
  const blocked = rule.shown === "blocked"
    ? "Configure a connection in the media worker to see what this selects."
    : value.connectionRef.trim() === "" ? "Choose a library connection to see what this selects."
      : !announced ? unannouncedWords(rule)
        : invalidWindow ? "Correct the dates to see what this selects." : null;

  // The tag paths learnt from the library's tag list (id -> path; null once a lookup by id
  // names it absent). Never draft state: learning a name dirties nothing.
  const [paths, setPaths] = useState({});
  const learn = useCallback((list) => {
    setPaths((known) => learnPaths(known, list));
  }, []);
  // The draft's tags not yet named (an edit's saved tags, beyond the first 20 the picker
  // reads) are looked up by id, so Review and the chips name them (C5).
  const connectionRef = value.connectionRef.trim();
  const unnamed = announced ? (value.tags ?? []).filter((ref) => !Object.hasOwn(paths, ref)) : EMPTY;
  const unnamedKey = JSON.stringify([connectionRef, unnamed]);
  useEffect(() => {
    if (unnamed.length === 0) return undefined;
    let current = true;
    readTagsById(connectionRef, unnamed).then((answers) => {
      if (current) setPaths((known) => answers.reduce((next, answer) => learnPaths(next, answer), known));
    });
    return () => { current = false; };
  }, [unnamedKey]); // `unnamed` is rebuilt each render; its content is the key

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
    keys,
    refs,
    availability: (key) => editedId(key) !== null && !namedSource(sources, editedId(key)) ? "missing" : "ok",
    steps,
    fieldStep,
    advancedFields: sourceAdvancedFields(layout),
    problemList,
    problems,
    confirm,
    handOff,
  });
  const { step } = flow;
  const write = useFlowWrite({ draft, confirm, failure: "Could not save Source" });
  const preview = usePreview({
    key: blocked === null && draft.id !== null ? `${draft.id}:${JSON.stringify(previewPayload)}` : null,
    payload: previewPayload,
    active: PREVIEWED.has(step),
    connection: value.connectionRef.trim(),
  });

  // Refresh re-runs a saved query (POST …/sources/{ref}/refresh) through the shared
  // card mutation, so the cards refresh exactly once after the write.
  const refreshSource = useCallback((sourceRef) => sourceRefresh.run(
    sourceRef,
    () => apiWrite(sourceRefreshPath(sourceRef), { method: "POST" }),
    (result) => {
      if (result.ok && result.status === 202) {
        return "Refresh requested. Check Status for the worker's latest result.";
      }
      if (result.status === 0 || result.status >= 500) {
        return "The refresh request outcome is unknown. Check the Source status before retrying.";
      }
      return `Refresh request failed: ${result.error ? codeWords(result.error) : `HTTP ${result.status}`}.`;
    },
  ), [sourceRefresh]);

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
          body: spec,
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
          <p>{`This removes Source ${name} from new selections. It does not delete anything in your photo library.`}</p>
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

  const panel = { preview, connections: rule.values, blocked };
  const stepProps = { value, patch: draft.patch, problems };
  const views = {
    library: () => <LibraryStep {...stepProps} rule={rule} />,
    tags: () => (
      <TagsStep
        {...stepProps}
        panel={panel}
        tags={announced ? { connection: value.connectionRef.trim(), paths, onLearn: learn } : null}
      />
    ),
    narrow: () => <NarrowStep {...stepProps} panel={panel} />,
    name: () => (
      <NameStep
        {...stepProps}
        rule={layout}
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
        <SourceReview value={value} onChange={flow.openField} rule={layout} paths={paths} spec={spec}
          panel={panel} />
      </>
    ),
  };

  return (
    <FlowFrame
      flow={flow}
      draft={draft}
      keys={keys}
      noun="Source"
      sectionLabel="Sources"
      confirm={confirm}
      problems={problems}
      handOff={handOff}
      newLabel="New Source"
      cards={<SourceCards sources={sources} now={now} connections={rule.values} reported={rule.reported} onRefresh={refreshSource} refreshingSources={sourceRefresh.pending} refreshFeedback={sourceRefresh.feedback} onEdit={(name, event) => flow.start(editKey(name), event)} onDelete={deleteSource} busy={write.busy} />}
      title={editingName === null ? "New Source" : `Edit Source ${editingName}`}
      steps={steps}
      formLabel="Configure a Source"
      heading={SOURCE_HEADINGS[step]}
      busy={write.busy}
      onWrite={onSave}
      writeLabel={editingName === null ? "Save Source" : "Save changes"}
      writeDisabled={write.busy || stale}
      problemsLabel="Source problems"
    >
      {step !== null && views[step]()}
    </FlowFrame>
  );
}

/**
 * The tag paths of the saved Sources' tags (console DDD §39): each announced connection's
 * saved tags are looked up by id once per snapshot of Sources (C5), so a tag is named
 * however long the library's list is, and one an `ok` list lacks reads as gone. A pending
 * or failed list names nothing gone, so those tags stay unknown, never "gone".
 */
function useSavedTagPaths(sources, connections, reported) {
  const [paths, setPaths] = useState({});
  const wanted = useMemo(() => {
    const byConnection = new Map();
    for (const source of sources) {
      const ref = source.spec?.connection_ref;
      if (!reported || !connections.includes(ref) || !(source.spec?.tags?.length > 0)) continue;
      byConnection.set(ref, [...(byConnection.get(ref) ?? []), ...source.spec.tags]);
    }
    return byConnection;
  }, [sources, connections, reported]);
  const wantedKey = JSON.stringify([...wanted]);
  useEffect(() => {
    let current = true;
    for (const [connection, tags] of wanted) {
      readTagsById(connection, tags).then((answers) => {
        if (current) setPaths((known) => answers.reduce((next, answer) => learnPaths(next, answer), known));
      });
    }
    return () => { current = false; };
  }, [wantedKey]); // `wanted` is rebuilt each render; its content is the key
  return paths;
}

/**
 * The saved Sources as summary cards: each by its plain logical name, with its status (for a
 * Source whose last refresh succeeded, that refresh as the library's report, sourceWords.js
 * `refreshFact`, said once), or that no refresh has succeeded, what it selects, its
 * connection, whether a tag it uses is gone, and Refresh, Edit and Delete. No thumbnails:
 * those live in the flow's preview (§37; card tiles are deferred, §44).
 *
 * @param {{sources: ReadonlyArray<object>, onRefresh: (sourceRef: string) => void,
 *          refreshingSources: Set<string>, refreshFeedback: Record<string, string|null>}} props
 */
function SourceCards({ sources, now, connections, reported, onRefresh, refreshingSources, refreshFeedback, onEdit, onDelete, busy }) {
  const paths = useSavedTagPaths(sources, connections, reported);
  if (sources.length === 0) {
    return <p className="showrunner__empty">No Sources yet.</p>;
  }
  return (
    <ul className="card-grid" role="list">
      {sources.map((source) => {
        const state = sourceState(source, now, false, connections);
        const refreshing = refreshingSources.has(source.source_ref);
        const feedback = Object.hasOwn(refreshFeedback, source.source_ref)
          ? refreshFeedback[source.source_ref] : null;
        const gone = (source.spec?.tags ?? []).some((tag) => Object.hasOwn(paths, tag) && paths[tag] === null);
        return <li key={source.source_ref} className="card-grid__item">
          <SummaryCard
            title={sourceName(source)}
            lines={[
              { label: "Status", value: state.label },
              // An ok Source's Status already states its refresh (sourceState's one fact).
              ...(state.state === "never-refreshed" || state.state === "ok" ? [] : [{
                label: "Refreshed",
                value: source.last_success == null ? "No successful refresh"
                  : source.status === "ok" ? factText(refreshFact(source, now, connections))
                    : "The last refresh failed (see Status)",
              }]),
              ...(source.diagnostics?.length
                ? [{ label: source.status === "ok" ? "Partial refresh" : "Issue",
                    value: sourceIssue(source, now) }]
                : []),
              ...(feedback
                ? [{
                    label: "Refresh request",
                    value: <span role="status" aria-live="polite">{feedback}</span>,
                  }]
                : []),
              { label: "Selects", value: selectionWords(source.spec, paths) },
              ...(gone ? [{ label: "Tags", value: TAG_GONE }] : []),
              { label: "Connection", value: source.spec?.connection_ref ?? "" },
            ]}
            actions={
              <>
                <button type="button" aria-label={`Refresh ${sourceName(source)}`} disabled={refreshing} onClick={() => onRefresh(source.source_ref)}>
                  {refreshing ? "Requesting refresh…" : "Refresh"}
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
      })}
    </ul>
  );
}

function sourceIssue(source, now) {
  const diagnostics = source.diagnostics ?? [];
  // One sentence per distinct code, then per distinct sentence (two codes may share a row).
  const details = [...new Set([...new Set(diagnostics.map((entry) => entry.code))]
    .slice(0, 3)
    .map(refusalIssue))]
    .join(" · ");
  if (source.status !== "ok") return details;
  const state = sourceState(source, now, false);
  const affected = Number(source.counts?.pending ?? 0) + Number(source.counts?.rejected ?? 0);
  const count = Number.isFinite(affected) && affected > 0 ? affected : diagnostics.length;
  return `Refresh succeeded with ${count} item${count === 1 ? "" : "s"} pending or rejected` +
    (details === "" ? "." : `: ${details}.`) +
    (state.state === "ok" ? " Usable items remain available." : "");
}
