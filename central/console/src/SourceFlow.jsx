import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

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
import { codeWords, mediaNow, sourceFilters, sourceState } from "./mediaHealth.js";
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
import { sourceRefreshPath } from "./mediaApi.js";
import { useCardMutation } from "./useCardMutation.js";

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
 * THE CONNECTION RULE (sourceFlowModel.js `connectionRule`) uses the worker's
 * reported connection IDs on every render. Until a worker reports them, saved
 * Sources guide a manual fallback marked as uncertain. The seed prefills a
 * single known connection.
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
    return inStepOrder(found, SOURCE_FIELD_STEP, SOURCE_STEPS);
  }, [value, editingName, rule]);
  const problems = useProblems(problemList);

  // Preview is deliberately transient: it belongs to this open draft and exact
  // filter payload, never to the saved Source draft itself.
  const previewPayload = useMemo(() => {
    const originalCapturedFrom = editingName !== null && value.capturedFrom === draft.seeded?.capturedFrom
      ? stored?.spec?.captured_from ?? null : undefined;
    const originalCapturedUntil = editingName !== null && value.capturedUntil === draft.seeded?.capturedUntil
      ? stored?.spec?.captured_until ?? null : undefined;
    const spec = buildSourceSpec({
      ...value,
      connectionRef: value.connectionRef.trim(),
      expectedRevision: null,
      originalCapturedFrom,
      originalCapturedUntil,
    });
    const { expected_revision: _revision, new_name: _newName, ...payload } = spec;
    return payload;
  }, [value, editingName, draft.seeded, stored]);
  const previewKey = JSON.stringify(previewPayload);
  const [previewRequest, setPreviewRequest] = useState(null);
  const [previewState, setPreviewState] = useState(null);
  const previewGeneration = useRef(0);
  const startedPreviewRequest = useRef(0);
  const previewRequestId = useRef(0);
  const requestPreview = useCallback((requestId = null) => {
    previewRequestId.current += 1;
    setPreviewRequest({
      id: previewRequestId.current,
      key: previewKey,
      draftId: draft.id,
      payload: previewPayload,
      requestId,
    });
  }, [previewKey, draft.id, previewPayload]);

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

  useEffect(() => {
    if (previewRequest === null || previewRequest.id <= startedPreviewRequest.current ||
        previewRequest.key !== previewKey || previewRequest.draftId !== draft.id ||
        step !== "include" || draft.id === null) return undefined;
    startedPreviewRequest.current = previewRequest.id;
    const generation = ++previewGeneration.current;
    const draftId = previewRequest.draftId;
    let cancelled = false;
    let timer = null;
    let requestId = previewRequest.requestId;
    let postPending = false;
    const current = () => !cancelled && generation === previewGeneration.current && draft.isOpen(draftId);
    const publish = (state) => {
      if (current()) setPreviewState({ ...state, key: previewKey, draftId });
    };
    const wait = () => new Promise((resolve) => { timer = window.setTimeout(resolve, 1000); });
    const run = async () => {
      if (requestId === null) {
        postPending = true;
        publish({ busy: true, message: "Requesting a match preview…", result: null, error: false });
        let response;
        try {
          response = await apiWrite("/v1/operator/source-previews", {
            method: "POST",
            body: previewRequest.payload,
          });
        } catch {
          postPending = false;
          publish({ busy: false, message: "The preview request outcome is unknown. Try again.", error: true });
          return;
        }
        postPending = false;
        if (!current()) return;
        if (!response.ok || response.status !== 202 || !response.data?.request_id) {
          const detail = response.error ? codeWords(response.error) : `HTTP ${response.status}`;
          publish({ busy: false, message: `Could not request a preview: ${detail}.`, error: true });
          return;
        }
        requestId = response.data.request_id;
      }
      publish({ busy: true, message: "Checking the photo library…", result: null, error: false });
      for (let attempt = 0; attempt < 80; attempt += 1) {
        await wait();
        if (!current()) return;
        let poll;
        try {
          poll = await apiWrite(`/v1/operator/source-previews/${encodeURIComponent(requestId)}`, { method: "GET" });
        } catch {
          publish({ busy: false, message: "Could not read the preview result. Try again.", error: true });
          return;
        }
        if (!current()) return;
        if (!poll.ok) {
          const detail = poll.error ? codeWords(poll.error) : `HTTP ${poll.status}`;
          publish({ busy: false, message: `Could not read the preview result: ${detail}.`, error: true });
          return;
        }
        if (poll.data?.status === "complete") {
          publish({ busy: false, result: poll.data, message: null, error: false });
          return;
        }
        if (poll.data?.status === "failed") {
          publish({ busy: false, message: `The preview failed: ${codeWords(poll.data.error ?? "unknown_error")}.`, error: true });
          return;
        }
        if (poll.data?.status !== "pending") {
          publish({ busy: false, message: "The preview returned an unknown status. Try again.", error: true });
          return;
        }
      }
      publish({ busy: false, timedOut: true, requestId, message: null, error: false });
    };
    run();
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
      if (requestId !== null || postPending) {
        setPreviewState((previous) => {
          if (previous?.key !== previewKey || previous?.draftId !== draftId || !previous.busy) {
            return previous;
          }
          return requestId !== null
            ? { ...previous, busy: false, timedOut: true, requestId, message: null }
            : { ...previous, busy: false, timedOut: false,
              message: "The preview request outcome is unknown. Request a new preview when ready.", error: true };
        });
      }
    };
  }, [previewRequest, previewKey, step, draft.id]);

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

  const previewMatches = previewState?.key === previewKey && previewState?.draftId === draft.id
    ? previewState : null;
  const invalidWindow = sourceProblems(value).some(({ field }) => field === "from" || field === "until");
  const canPreview = value.connectionRef.trim() !== "" && !rule.selectedUnavailable &&
    rule.shown !== "blocked" && !invalidWindow;
  const previewHint = rule.selectedUnavailable
    ? "Choose a connection currently configured in the media worker."
    : rule.shown === "blocked"
      ? "Configure a connection in the media worker before previewing."
      : value.connectionRef.trim() === ""
        ? rule.shown === "chooser"
          ? "Choose a configured connection above before previewing."
          : "Choose or enter a connection on the Name step before previewing."
        : invalidWindow ? "Correct the capture date range before previewing." : null;
  const stepProps = { value, patch: draft.patch, problems };
  const views = {
    include: () => (
      <IncludeStep
        {...stepProps}
        rule={rule}
        preview={{
          ...(previewMatches ?? {}),
          canRequest: canPreview,
          hint: previewHint,
          onRequest: () => requestPreview(previewMatches?.timedOut ? previewMatches.requestId : null),
        }}
      />
    ),
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
      cards={<SourceCards sources={sources} now={now} onRefresh={refreshSource} refreshingSources={sourceRefresh.pending} refreshFeedback={sourceRefresh.feedback} onEdit={(name, event) => flow.start(editKey(name), event)} onDelete={deleteSource} busy={write.busy} />}
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
 * @param {{sources: ReadonlyArray<object>, onRefresh: (sourceRef: string) => void,
 *          refreshingSources: Set<string>, refreshFeedback: Record<string, string|null>}} props
 */
function SourceCards({ sources, now, onRefresh, refreshingSources, refreshFeedback, onEdit, onDelete, busy }) {
  if (sources.length === 0) {
    return <p className="showrunner__empty">No Sources yet.</p>;
  }
  return (
    <ul className="card-grid" role="list">
      {sources.map((source) => {
        const state = sourceState(source, now, false);
        const refreshing = refreshingSources.has(source.source_ref);
        const feedback = Object.hasOwn(refreshFeedback, source.source_ref)
          ? refreshFeedback[source.source_ref] : null;
        return <li key={source.source_ref} className="card-grid__item">
          <SummaryCard
            title={sourceName(source)}
            lines={[
              { label: "Status", value: state.label },
              ...(state.state === "never-refreshed" ? [] : [{
                label: "Refreshed",
                value: source.last_success
                  ? `Last refreshed ${new Date(source.last_success * 1000).toLocaleString()}`
                  : "No successful refresh",
              }]),
              ...(source.status !== "ok" && source.diagnostics?.length
                ? [{ label: "Issue", value: sourceIssue(source.diagnostics) }]
                : []),
              ...(feedback
                ? [{
                    label: "Refresh request",
                    value: <span role="status" aria-live="polite">{feedback}</span>,
                  }]
                : []),
              { label: "Includes", value: includesWords(source.spec) },
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
