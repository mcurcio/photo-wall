import { useEffect, useMemo, useRef, useState } from "react";

import {
  type LibraryAlbum,
  type LibraryLookups,
  type LibraryPerson,
  type LibraryTag,
  type Place,
  type SourceQuery,
  SourceQueryEditor,
  activeFilters,
  defaultQuery,
} from "../domain/source-query-editor";
import { type SourcePoolViewData, SourcePoolView, poolWords } from "../domain/source-pool-view";
import { fitLine, type FitQuestion, PhotoFitBoard, useFitRead } from "../domain/photo-fit-board";
import { type FitAnalysis, percentWords, type SceneUse } from "../domain/photo-fit";
import {
  albumsById,
  peopleById,
  searchAlbums,
  searchPeople,
  searchPlaces,
  searchTags,
  tagsById,
  useLibraryConnections,
} from "../libraryApi.js";
import { createDraft, saveDraft, setDraftQuery } from "../photoSourcesApi.js";
import { EmptyState } from "../patterns/entity-page";
import { EditorPage } from "../patterns/editor-page";
import { EntityHeader } from "../patterns/entity-header";
import { Note } from "../patterns/fact-row";
import type { PickerAnswer } from "../patterns/item-picker";
import { SaveBar } from "../patterns/save-bar";
import { useDraftPool } from "../useDraftPool.js";
import { sourceCheckMessage, useSourceCheck } from "../useSourceCheck.js";
import { Button } from "../ui/button";
import { Field, TextInput } from "../ui/field";
import { Select } from "../ui/select";
import { Link } from "../ui/link";
import { Inline, Stack } from "../ui/stack";

/** How long typing settles before the query is sent: every change re-reads the pool. */
const SEND_DELAY_MS = 400;
const CONNECTIONS_CADENCE_MS = 30000;

interface ListAnswer<T> { status: string; items: T[] }
/** libraryApi.js's answer to a list read: `answer` when `ok`. */
interface Listed { ok: boolean; answer?: unknown }

/** A list read as a picker's answer: its items, and a line when the list is not current. */
function picked<T>(result: Listed): PickerAnswer<T> {
  if (!result.ok) return { items: [], note: "Photo Wall could not read this list just now." };
  const { status, items } = result.answer as ListAnswer<T>;
  const note = status === "ok" ? null
    : status === "pending" ? "Not listed yet: Photo Wall lists Immich's albums, people, places and tags at each check of the connection."
      : "The last listing failed; this is the list from before.";
  return { items, note };
}

function named<T>(result: Listed): readonly T[] {
  return result.ok ? (result.answer as ListAnswer<T>).items : [];
}

/** The library's lists for one connection, as the editor's pickers read them. */
function lookupsFor(connection: string): LibraryLookups {
  return {
    albums: {
      search: async (q) => picked<LibraryAlbum>(await searchAlbums(connection, q)),
      byId: async (ids) => named<LibraryAlbum>(await albumsById(connection, ids)),
    },
    people: {
      search: async (q) => picked<LibraryPerson>(await searchPeople(connection, q)),
      byId: async (ids) => named<LibraryPerson>(await peopleById(connection, ids)),
    },
    tags: {
      search: async (q) => picked<LibraryTag>(await searchTags(connection, q)),
      byId: async (ids) => named<LibraryTag>(await tagsById(connection, ids)),
    },
    places: { search: async (q) => picked<Place>(await searchPlaces(connection, q)) },
  };
}

/** What is wrong with the dates as chosen, or null: such a query is not sent. */
export function datesProblem(query: SourceQuery): string | null {
  const taken = query.taken;
  if (taken?.kind === "last" && taken.years + taken.months <= 0) return "Choose at least one month.";
  if (taken?.kind === "between" && taken.start !== null && taken.end !== null && !(taken.start < taken.end)) {
    return "The first day must be on or before the last.";
  }
  return null;
}

/** A save refusal (central/photo_library_routes.py) in words. */
function saveWords(code: string, name: string): string {
  switch (code) {
    case "source_name_exists": return `A Source named ${name} exists. Choose another name, or edit that Source.`;
    case "invalid_source_name": return "A name starts with a letter or digit, then letters, digits, dots, dashes or underscores.";
    case "source_limit": return "Photo Wall holds as many Sources as it can. Delete one first.";
    case "draft_unknown": return "This unsaved Source expired. Discard it and start again.";
    case "source_not_found": return "The Source you were changing was deleted meanwhile.";
    case "unreachable": return "Photo Wall did not answer. Try again.";
    default: return `Photo Wall did not save this (${code}).`;
  }
}

export interface EditedSource {
  name: string;
  sourceRef: string;
  /** Null for a Source made before 4 and not converted: it starts again from every photo. */
  query: SourceQuery | null;
}

export interface PhotoSourcePageProps {
  /** The saved Source being changed, or null for a new one. */
  editing: EditedSource | null;
  /** Whom the Source is made for when another page asked for it ("your Scene"), or null. */
  forLabel: string | null;
  onSaved: (saved: { sourceRef: string; name: string }) => void;
  onDiscard: () => void;
  /** Called with true once anything is changed (the sidebar marks the section). */
  onChanged: (changed: boolean) => void;
  /** The stored Scenes that use this Source: its Fit board can judge as one of them does. */
  scenes?: readonly SceneUse[];
}

const HOUSE = "";

/** What the Photo source editor's board is judged at, in words. */
function judgedWords(scene: SceneUse | null): (analysis: FitAnalysis) => string {
  return (analysis) => (scene === null
    ? `Judged on every Frame at the house crop limit, ${percentWords(analysis.crop_limit)}: a Source has no Frames of its own.`
    : `Judged on every Frame as Scene ${scene.sceneId} judges it: crop limit ${percentWords(analysis.crop_limit)}.`);
}

/**
 * The Photo source editor (`#/sources/new/query`, `#/sources/<name>/edit/query`; design-language
 * T4; console design #13-#33): New Source and Edit both open an unsaved Source, whose check is
 * real: its photos join the pool as Immich is read. Every change sends the query and the pool
 * view starts again; the name and Save work at any time (C8), and Save carries the pool over.
 */
export function PhotoSourcePage({ editing, forLabel, onSaved, onDiscard, onChanged, scenes = [] }: PhotoSourcePageProps) {
  const library = useLibraryConnections({ cadenceMs: CONNECTIONS_CADENCE_MS });
  const connections = useMemo(
    () => (library.connections ?? []).map((c: { connection_ref: string; name: string }) => ({ connection_ref: c.connection_ref, name: c.name })),
    [library.connections],
  );
  const first = connections[0]?.connection_ref ?? null;
  const [query, setQuery] = useState<SourceQuery | null>(editing?.query ?? null);
  const [name, setName] = useState(editing?.name ?? "");
  const [draftRef, setDraftRef] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const sent = useRef<string | null>(null);

  // A Source made before 4 (no query) starts again from every photo of the first connection.
  useEffect(() => {
    if (query === null && first !== null) setQuery(defaultQuery(first));
  }, [query, first]);

  // The unsaved Source: made once, from the first query.
  const creating = useRef(false);
  useEffect(() => {
    if (query === null || draftRef !== null || creating.current) return;
    creating.current = true;
    void createDraft(query, editing?.sourceRef ?? null).then((result: { ok: boolean; created?: { draft_ref: string }; error?: string }) => {
      if (result.ok && result.created) {
        sent.current = JSON.stringify(query);
        setDraftRef(result.created.draft_ref);
      } else {
        creating.current = false;
        setProblem(result.error === "connection_unknown"
          ? "This Source's photo library was removed. Choose another."
          : "Photo Wall could not start reading this Source. Try again.");
      }
    });
  }, [query, draftRef, editing]);

  const pool = useDraftPool({ draftRef, sourceRef: draftRef === null ? editing?.sourceRef ?? null : null });
  const { restart } = pool;

  // Every change sends the query once typing settles; the pool view starts again.
  const dates = query === null ? null : datesProblem(query);
  useEffect(() => {
    if (draftRef === null || query === null || dates !== null) return undefined;
    const text = JSON.stringify(query);
    if (text === sent.current) return undefined;
    const timer = setTimeout(() => {
      sent.current = text;
      void setDraftQuery(draftRef, query).then((result: { ok: boolean }) => {
        if (result.ok) restart();
        else setProblem("Photo Wall did not take the last change. Change it again to retry.");
      });
    }, SEND_DELAY_MS);
    return () => clearTimeout(timer);
  }, [draftRef, query, dates, restart]);

  const connectionRef = query?.connection_ref ?? null;
  const lookups = useMemo(() => (connectionRef === null ? null : lookupsFor(connectionRef)), [connectionRef]);

  const change = (next: SourceQuery) => {
    setQuery(next);
    setProblem(null);
    onChanged(true);
  };

  // How the photos fit the Frames (follow-ups C1): the unsaved Source's photos (or, before it
  // exists, the saved one's) on every Frame, at the house crop limit or as a Scene using it.
  const [judgeAt, setJudgeAt] = useState(HOUSE);
  const judgingScene = scenes.find((scene) => scene.sceneId === judgeAt) ?? null;
  const fitQuestion = useMemo((): FitQuestion | null => {
    const selection = draftRef !== null ? { drafts: [draftRef] }
      : editing !== null ? { sources: [editing.sourceRef] } : null;
    return selection === null ? null : { selection, frames: null, crop_limit: judgingScene?.cropLimit ?? null };
  }, [draftRef, editing, judgingScene]);
  const check = useSourceCheck(editing?.sourceRef ?? null);
  const view = pool.view as SourcePoolViewData | null;
  // The same question is asked again whenever the pool read under it moves on (Immich read
  // further, or the query changed and a new pool started).
  const fit = useFitRead(fitQuestion, { refreshKey: JSON.stringify([view?.in_pool ?? null, view?.check ?? null]) });
  const checkNote = editing === null ? null
    : sourceCheckMessage(check.feedback[editing.sourceRef], view?.check ?? null);

  const save = async () => {
    const trimmed = name.trim();
    if (draftRef === null) return;
    if (trimmed === "") {
      setProblem("Name this Source before saving it.");
      return;
    }
    if (query !== null && JSON.stringify(query) !== sent.current) {
      if (dates !== null) {
        setProblem(dates);
        return;
      }
      sent.current = JSON.stringify(query);
      const result = await setDraftQuery(draftRef, query);
      if (!result.ok) {
        setProblem("Photo Wall did not take the last change. Try again.");
        return;
      }
    }
    setSaving(true);
    const result = await saveDraft(draftRef, trimmed);
    setSaving(false);
    if (result.ok) {
      onSaved({ sourceRef: result.saved.source_ref, name: trimmed });
    } else {
      setProblem(saveWords(result.error, trimmed));
    }
  };

  // One title: "New Photo source", or the Source's own name.
  const title = editing === null ? "New Photo source" : editing.name;
  if (library.connections !== null && connections.length === 0) {
    return (
      <Stack>
        <EntityHeader title={title} />
        <EmptyState>A Photo source reads your photo library, and none is connected yet.</EmptyState>
        <Link href="#/photo-library">Connect your photo library</Link>
        <Inline><Button variant="ghost" onClick={onDiscard}>Back</Button></Inline>
      </Stack>
    );
  }
  const filters = query === null ? [] : activeFilters(query).map(({ label, cleared }) => ({ label, onClear: () => change(cleared) }));
  return (
    <EditorPage
      header={(
        <EntityHeader title={title}>
          {forLabel !== null && <Note>{`For ${forLabel}: saving returns there with this Source chosen.`}</Note>}
        </EntityHeader>
      )}
      form={(
        <>
          <Field label="Name" help="What Scenes and Show now call this Source.">
            <TextInput value={name} width="long" onChange={(next) => { setName(next); onChanged(true); }} />
          </Field>
          {query === null || lookups === null ? <Note>Loading…</Note> : (
            <SourceQueryEditor connections={connections} value={query} onChange={change}
              lookups={lookups} datesProblem={dates} />
          )}
        </>
      )}
      previewTitle="Photos in this Source"
      previewLine={[view === null ? "Reading the pool…" : poolWords(view), fitLine(fit.state)]
        .filter((part) => part !== null).join(" · ")}
      preview={() => (
        <Stack>
          {scenes.length > 0 && (
            <Field label="Judge as" help="A Source has no Frames or crop limit of its own: judge it as the house does, or as a Scene that uses it.">
              <Select value={judgeAt} onChange={setJudgeAt} options={[
                { value: HOUSE, label: "The house crop limit" },
                ...scenes.map((scene) => ({ value: scene.sceneId, label: `Scene ${scene.sceneId}` })),
              ]} />
            </Field>
          )}
          <PhotoFitBoard read={fit} size="full" lead="shapes" collapse={false}
            title="What these photos are, and which Frames can show them" judged={judgedWords(judgingScene)} />
          <SourcePoolView view={view} pages={pool.pages} hasMore={pool.hasMore} onLoadMore={() => void pool.loadMore()}
            loadingMore={pool.loadingMore} error={pool.error} filters={filters} />
        </Stack>
      )}
      saveBar={(
        <SaveBar
          label="Source actions"
          message={(problem !== null || checkNote !== null) && (
            <>
              {problem !== null && <Note live>{problem}</Note>}
              {checkNote !== null && <Note live>{checkNote}</Note>}
            </>
          )}
        >
          <Button variant="primary" disabled={saving || draftRef === null} onClick={() => void save()}>
            {saving ? "Saving…" : editing === null ? "Save Photo source" : "Save changes"}
          </Button>
          {editing !== null && (
            <Button disabled={check.pending !== null} onClick={() => void check.run(editing.sourceRef, view?.check ?? null)}>
              Check now
            </Button>
          )}
          <Button variant="ghost" disabled={saving} onClick={onDiscard}>
            {forLabel !== null ? `Back to ${forLabel} without saving` : "Discard changes"}
          </Button>
        </SaveBar>
      )}
    />
  );
}
