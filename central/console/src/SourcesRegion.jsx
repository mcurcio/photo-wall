import React, { useEffect, useMemo, useRef, useState } from "react";

import { apiWrite } from "./apiWrite.js";
import { useHandOffTo } from "./flow/useHandOff.js";
import { scenesUsingSource } from "./domain/photo-fit.ts";
import { FitBoardFor } from "./domain/photo-fit-board.tsx";
import { SourceCard } from "./domain/source-card.tsx";
import { mediaNow } from "./mediaHealth.js";
import { PhotoSourcePage } from "./pages/photo-source-page.tsx";
import { MediaCardList, MediaCardListItem } from "./patterns/media-card.tsx";
import { UndoToast } from "./patterns/undo-toast.tsx";
import { formatRoute } from "./routes.js";
import { namedSource, sourceName } from "./sourceNames.js";
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
 * The Sources section (`#/sources`; roadmap 4, K2): the saved Photo sources as cards (each with
 * its photos, counts and check state; its name opens its editor; More holds Check now and
 * Delete, which Undo can take back), and the Photo source editor (pages/photo-source-page.tsx)
 * for a new Source or an edit. While the editor shows, its title is the page's one heading.
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
      scenes={editing === null ? [] : scenesUsingSource(snapshot, editing.source_ref)}
    />
  );

  return (
    <section className="showrunner__region" role="region" aria-label="Sources">
      {routeKey === null && (
        <>
          <h2 className="showrunner__region-title">Sources</h2>
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
 * A Source's card's fit line (follow-ups C1): how its photos fit every Frame at the house crop
 * limit, by shape and size (the analysis's shape tier: a list asks for many at once), opening
 * the board in a sheet.
 */
function SourceFitLine({ sourceRef, name }) {
  const question = useMemo(() => ({ selection: { sources: [sourceRef] }, frames: null, crop_limit: null }), [sourceRef]);
  return (
    <FitBoardFor question={question} tier="shape" size="line" title={`How ${name}'s photos fit the Frames`}
      judged={{ at: "source-card" }} />
  );
}

/** The words of a refused delete, or null when it was taken. */
function deleteRefusal(result, name) {
  if (result.ok) return null;
  if (result.error === "source_in_use") {
    const scenes = result.data?.scene_ids;
    return Array.isArray(scenes) && scenes.length > 0
      ? `Source ${name} was not deleted: Scenes ${scenes.join(", ")} use it. Edit them to choose another Source first.`
      : `Source ${name} was not deleted: a Scene uses it. Edit the Scene to choose another Source first.`;
  }
  if (result.error === "source_revision_conflict") {
    return `Source ${name} was not deleted: it changed meanwhile. Look at it again.`;
  }
  return `Source ${name} was not deleted: ${result.error ?? result.status}.`;
}

/**
 * The saved Sources as cards (the object list's cards; MediaCard): each with its first four photos,
 * its photo and video counts, its check's state in the one chip, and More (Check now, Delete).
 * Its name opens its editor. Delete takes effect when its Undo toast ends (10 s): the card goes
 * at once, Undo brings it back, and nothing is sent until then. A refusal (a Scene uses it)
 * brings the card back and says why.
 */
function SourceList({ sources, now, navigate, resume }) {
  const check = useSourceCheck("sources-list");
  // The delete waiting for its Undo toast to end, and the last refusal's words.
  const [pending, setPending] = useState(/** @type {{source: object, name: string}|null} */ (null));
  const [refused, setRefused] = useState(/** @type {string|null} */ (null));
  const commit = async ({ source, name }) => {
    setPending((current) => (current?.source === source ? null : current));
    const result = await apiWrite(
      `/v1/operator/source-names/${encodeURIComponent(name)}?expected_revision=${source.revision}`,
      { method: "DELETE" },
    ).catch(() => ({ ok: false, status: 0, error: "unreachable" }));
    setRefused(deleteRefusal(result, name));
    if (result.ok) void refreshSnapshot();
  };
  const { refresh: refreshSnapshot } = useSnapshot();
  const remove = (source) => {
    setRefused(null);
    // One delete waits at a time: a second one makes the first final now.
    if (pending !== null) void commit(pending);
    setPending({ source, name: sourceName(source) });
  };
  const shown = sources.filter((source) => source !== pending?.source);
  return (
    <>
      {refused !== null && <p className="flow__status-line" role="status">{refused}</p>}
      <p>
        <button type="button" className="flow__new"
          onClick={() => navigate({ section: "sources", flow: "new", step: EDITOR_STEP })}>
          {resume ? "Continue the new Photo source" : "New Photo source"}
        </button>
      </p>
      {shown.length === 0 ? <p className="showrunner__empty">No Sources yet.</p> : (
        <MediaCardList label="Photo sources">
          {shown.map((source) => {
            const name = sourceName(source);
            return (
              <MediaCardListItem key={source.source_ref}>
                <SourceCard
                  source={source}
                  name={name}
                  now={now}
                  href={formatRoute({ section: "sources", id: name, flow: "edit", step: EDITOR_STEP })}
                  note={sourceCheckMessage(check.feedback[source.source_ref], source.check)}
                  actions={[
                    {
                      label: "Check now",
                      disabled: check.pending !== null,
                      onAction: () => void check.run(source.source_ref, source.check ?? null),
                    },
                    { label: "Delete", onAction: () => remove(source) },
                  ]}
                  fit={<SourceFitLine sourceRef={source.source_ref} name={name} />}
                />
              </MediaCardListItem>
            );
          })}
        </MediaCardList>
      )}
      {pending !== null && (
        <UndoToast
          key={pending.source.source_ref}
          message={`Deleted Source ${pending.name}.`}
          onUndo={() => setPending(null)}
          onExpire={() => void commit(pending)}
        />
      )}
    </>
  );
}
