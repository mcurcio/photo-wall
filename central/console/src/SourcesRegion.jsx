import React, { useEffect, useRef, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { useConfirm } from "./ConfirmAction.jsx";
import { SummaryCard } from "./flow/SummaryCard.jsx";
import { useHandOffTo } from "./flow/useHandOff.js";
import { mediaNow, sourceState } from "./mediaHealth.js";
import { PhotoSourcePage } from "./pages/photo-source-page.tsx";
import { namedSource, sourceName } from "./sourceNames.js";
import { queryWords } from "./sourceWords.js";
import { sourceCheckMessage, useSourceCheck } from "./useSourceCheck.js";
import { useSnapshot } from "./useSnapshot.js";

const EMPTY = [];
const NEW = "new";
// The editor's one route step: `#/sources/new/query`, `#/sources/<name>/edit/query`. Any other
// step a bookmark names opens the same editor.
export const EDITOR_STEP = "query";

/** The editor a sources route names: "new", a Source's name, or null for the list. */
function editorKey(route) {
  if (route?.section !== "sources" || route.flow === undefined) return null;
  return route.flow === "edit" ? route.id : NEW;
}

/**
 * The Sources section (`#/sources`; roadmap 4, K2): the saved Photo sources, each with its
 * state in words (mediaHealth.js `sourceState`, from its check), what it includes, Check now,
 * Edit and Delete, and the Photo source editor (pages/photo-source-page.tsx) for a new Source or
 * an edit.
 *
 * THE EDITOR stays mounted while it is open, whatever the route (the shell keeps Show sections
 * mounted, rule 2), so a section change or a visit to the list keeps what was typed; it closes on
 * Save and on Discard. A new route key (another Source, or New Source) opens a fresh one.
 *
 * INLINE (flow/handOff.js): when the Scene editor hands off to "sources", a new editor opens and
 * says whom it is for; Save returns there with `{sourceRef}`, Back returns with nothing.
 *
 * Photo Wall selects media that lives in the photo library; it never uploads, edits or deletes
 * anything there.
 *
 * @param {{snapshot: object|null, route: import("./routes.js").Route|null,
 *          navigate: (route: import("./routes.js").Route, options?: {replace?: boolean}) => void,
 *          markDraft: (section: string, dirty: boolean) => void,
 *          handOffs: ReturnType<typeof import("./flow/useHandOff.js").useHandOff>}} props
 */
export function SourcesRegion({ snapshot, route, navigate, markDraft, handOffs }) {
  const sources = snapshot?.media?.sources ?? EMPTY;
  const { refresh } = useSnapshot();
  const handOff = useHandOffTo(handOffs, "sources");
  const routeKey = editorKey(route);
  const [open, setOpen] = useState(/** @type {{key: string, epoch: number, changed: boolean}|null} */ (null));
  const epoch = useRef(0);
  const openKey = open?.key ?? null;
  // The last Save's line on the list ("Saved Source spring."), until another editor opens.
  const [saved, setSaved] = useState(/** @type {string|null} */ (null));

  // A route naming another editor opens it; the same key resumes the open one. The key of an
  // editor just closed does not reopen it while its route is still shown.
  const closed = useRef(null);
  useEffect(() => {
    if (routeKey !== closed.current) closed.current = null;
    if (routeKey !== null && routeKey !== openKey && routeKey !== closed.current) {
      epoch.current += 1;
      setOpen({ key: routeKey, epoch: epoch.current, changed: false });
      setSaved(null);
    }
  }, [routeKey, openKey]);

  // A hand-off addressed here opens a fresh new editor, once per hand-off.
  const handed = useRef(null);
  const handOffId = handOff?.id ?? null;
  useEffect(() => {
    if (handOffId !== null && handOffId !== handed.current) {
      handed.current = handOffId;
      epoch.current += 1;
      setOpen({ key: NEW, epoch: epoch.current, changed: false });
      navigate({ section: "sources", flow: "new", step: EDITOR_STEP });
    }
  }, [handOffId, navigate]);

  useEffect(() => {
    markDraft("sources", open?.changed === true);
  }, [open, markDraft]);

  const close = (result) => {
    const here = route?.section === "sources";
    closed.current = routeKey;
    setOpen(null);
    if (handOff !== null && handOff.settle(result, { show: here })) return;
    if (here) navigate({ section: "sources" });
  };

  const editing = open === null || open.key === NEW ? null : namedSource(sources, open.key) ?? null;
  const editor = open === null ? null : open.key !== NEW && editing === null ? (
    <p className="showrunner__empty" role="status">{`There is no Source named ${open.key}.`}</p>
  ) : (
    <PhotoSourcePage
      key={open.epoch}
      editing={editing === null ? null : { name: sourceName(editing), sourceRef: editing.source_ref, query: editing.query ?? null }}
      forLabel={open.key === NEW && handOff !== null ? handOff.label : null}
      onSaved={({ sourceRef, name }) => {
        void refresh().catch(() => {});
        setSaved(`Saved Source ${name}.`);
        close({ sourceRef });
      }}
      onDiscard={() => close(null)}
      onChanged={(changed) => setOpen((current) => (current === null || current.changed === changed ? current : { ...current, changed }))}
    />
  );

  return (
    <section className="showrunner__region" role="region" aria-label="Sources">
      <h2 className="showrunner__region-title">Sources</h2>
      {routeKey === null && (
        <>
          <p className="showrunner__region-note">
            A Photo source asks Immich for photos: albums, people, a place, dates, favourites and tags,
            minus what you leave out. Photo Wall never uploads, edits or deletes anything there.
          </p>
          {saved !== null && <p className="flow__status-line" role="status">{saved}</p>}
          <SourceList sources={sources} now={mediaNow(snapshot)} navigate={navigate} resume={open?.key === NEW} />
        </>
      )}
      {editor !== null && <div hidden={routeKey === null}>{editor}</div>}
    </section>
  );
}

/**
 * The saved Sources as cards: each by its name, its state from its check, what it includes, and
 * Check now, Edit and Delete. Delete is confirmed, and refused while a Scene uses the Source.
 */
function SourceList({ sources, now, navigate, resume }) {
  const check = useSourceCheck("sources-list");
  const newRef = useRef(null);
  const confirm = useConfirm(() => newRef.current?.focus());
  const remove = (source, event) => {
    const name = sourceName(source);
    confirm.open(event, {
      key: `delete-source:${name}`,
      title: `Delete Source ${name}?`,
      confirmLabel: "Confirm delete",
      body: <p>{`Photo Wall stops using Source ${name}. Nothing in Immich changes.`}</p>,
      run: async () => {
        const result = await apiWrite(
          `/v1/operator/source-names/${encodeURIComponent(name)}?expected_revision=${source.revision}`,
          { method: "DELETE" },
        );
        if (result.ok) return { state: "done", message: `Source ${name} deleted.` };
        if (result.error === "source_in_use") {
          const scenes = result.data?.scene_ids;
          return { state: "refused", message: Array.isArray(scenes) && scenes.length > 0
            ? `Used by Scenes ${scenes.join(", ")}. Edit them to choose another Source first.`
            : "A Scene uses this Source. Edit the Scene to choose another Source first." };
        }
        if (result.error === "source_revision_conflict") {
          return { state: "changed", message: "This Source changed. Close this dialog and look at it again." };
        }
        return { state: "refused", message: `Could not delete Source ${name}: ${result.error ?? result.status}.` };
      },
    });
  };
  return (
    <>
      {confirm.confirmation("flow__status-line")}
      <p>
        <button ref={newRef} type="button" className="flow__new"
          onClick={() => navigate({ section: "sources", flow: "new", step: EDITOR_STEP })}>
          {resume ? "Continue the new Source" : "New Source"}
        </button>
      </p>
      {sources.length === 0 ? <p className="showrunner__empty">No Sources yet.</p> : (
        <ul className="card-grid" role="list">
          {sources.map((source) => {
            const name = sourceName(source);
            const state = sourceState(source, now);
            const note = sourceCheckMessage(check.feedback[source.source_ref], source.check);
            return (
              <li key={source.source_ref} className="card-grid__item">
                <SummaryCard
                  title={name}
                  lines={[
                    { label: "State", value: <span className={`health--${state.severity}`}>{state.label}</span> },
                    { label: "Includes", value: queryWords(source.query) },
                    ...(note === null ? [] : [{ label: "Check now", value: <span role="status">{note}</span> }]),
                  ]}
                  actions={(
                    <>
                      <button type="button" aria-label={`Check ${name} now`} disabled={check.pending !== null}
                        onClick={() => void check.run(source.source_ref, source.check ?? null)}>
                        Check now
                      </button>
                      <button type="button" aria-label={`Edit Source ${name}`}
                        onClick={() => navigate({ section: "sources", id: name, flow: "edit", step: EDITOR_STEP })}>
                        Edit
                      </button>
                      <button type="button" aria-label={`Delete Source ${name}`} onClick={(event) => remove(source, event)}>
                        Delete
                      </button>
                    </>
                  )}
                />
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}
