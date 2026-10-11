import { Note } from "../patterns/fact-row";
import { ItemPicker, type PickerAnswer } from "../patterns/item-picker";
import { Field, NumberInput, TextInput } from "../ui/field";
import { Section } from "../ui/section";
import { SegmentedControl } from "../ui/segmented-control";
import { Select } from "../ui/select";
import { Inline, Stack } from "../ui/stack";
import { countText } from "../timeWords.js";
import { Switch } from "../ui/switch";

/*
 * A Photo source's query (central/photo_library/model.py `SourceQuery`, the one home of the
 * shape; console design #13-#33): one include query plus exclusion queries, which Immich answers
 * and Photo Wall only subtracts. Several albums, people or tags mean all of them (C3); "any of
 * them" is a second Source on the Scene. Archived, hidden and locked photos are never included:
 * one fixed line, not a switch (C5).
 */

export type MediaType = "photo" | "video" | "live_photo";
export type TakenRange =
  | { kind: "between"; start: number | null; end: number | null }
  | { kind: "last"; years: number; months: number };
export interface Place { city: string | null; state: string | null; country: string }
export interface Exclusions { albums: string[]; people: string[]; tags: string[]; screenshots: boolean }
export interface SourceQuery {
  connection_ref: string;
  albums: string[];
  people: string[];
  place: Place | null;
  taken: TakenRange | null;
  favourites_only: boolean;
  tags: string[];
  media_types: MediaType[];
  partners: boolean;
  exclude: Exclusions;
}

export interface LibraryAlbum { album_ref: string; name: string; photos: number }
export interface LibraryPerson { person_ref: string; name: string }
export interface LibraryTag { tag_ref: string; path: string; name: string; parent_ref: string | null }

/** A list's search and its by-id read, for one connection. */
export interface Lookup<T> {
  search: (q: string) => Promise<PickerAnswer<T>>;
  byId: (ids: readonly string[]) => Promise<readonly T[]>;
}

/** What the editor reads from the library's lists (the worker lists them; Central serves them). */
export interface LibraryLookups {
  albums: Lookup<LibraryAlbum>;
  people: Lookup<LibraryPerson>;
  tags: Lookup<LibraryTag>;
  places: { search: (q: string) => Promise<PickerAnswer<Place>> };
}

/** A new Source's query: every photo, video and live photo of `connection`, no screenshots. */
export function defaultQuery(connection: string): SourceQuery {
  return {
    connection_ref: connection,
    albums: [],
    people: [],
    place: null,
    taken: null,
    favourites_only: false,
    tags: [],
    media_types: ["live_photo", "photo", "video"],
    partners: false,
    exclude: { albums: [], people: [], tags: [], screenshots: true },
  };
}

const ALL_OF_THEM = "All of these must be in the photo. For any of them, add another Source to the Scene.";
const MEDIA: { type: MediaType; label: string }[] = [
  { type: "photo", label: "Photos" },
  { type: "live_photo", label: "Live photos" },
  { type: "video", label: "Videos" },
];

const albumKey = (album: LibraryAlbum) => album.album_ref;
const albumWords = (album: LibraryAlbum) => `${album.name} (${countText(album.photos)})`;
const personKey = (person: LibraryPerson) => person.person_ref;
const personWords = (person: LibraryPerson) => person.name;
const tagKey = (tag: LibraryTag) => tag.tag_ref;
const tagWords = (tag: LibraryTag) => tag.path || tag.name;
const placeKey = (place: Place) => JSON.stringify([place.city, place.state, place.country]);
const placeWords = (place: Place) => [place.city, place.state, place.country].filter(Boolean).join(", ");

/** A local day as the date field shows it ("2024-03-01"), from a Central time. */
function dayText(epoch: number | null): string {
  if (epoch === null) return "";
  const date = new Date(epoch * 1000);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** The start of a typed local day, `days` later, as a Central time; null for no day. */
function dayStart(text: string, days = 0): number | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
  if (match === null) return null;
  return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]) + days).getTime() / 1000;
}

type TakenChoice = "any" | "between" | "last";

export interface SourceQueryEditorProps {
  connections: readonly { connection_ref: string; name: string }[];
  value: SourceQuery;
  onChange: (next: SourceQuery) => void;
  lookups: LibraryLookups;
  /** A problem with the dates as typed, or null. */
  datesProblem?: string | null;
}

/** SourceQueryEditor: the Photo source editor's Include and Exclude sections (#14-#33). */
export function SourceQueryEditor({ connections, value, onChange, lookups, datesProblem = null }: SourceQueryEditorProps) {
  const set = (patch: Partial<SourceQuery>) => onChange({ ...value, ...patch });
  const exclude = (patch: Partial<Exclusions>) => set({ exclude: { ...value.exclude, ...patch } });
  const taken = value.taken;
  const choice: TakenChoice = taken === null ? "any" : taken.kind;
  const chooseTaken = (next: TakenChoice) => set({
    taken: next === "any" ? null : next === "between" ? { kind: "between", start: null, end: null } : { kind: "last", years: 1, months: 0 },
  });
  const toggleMedia = (type: MediaType, on: boolean) => {
    const types = on ? [...value.media_types, type] : value.media_types.filter((t) => t !== type);
    set({ media_types: [...new Set(types)].sort() as MediaType[] });
  };
  const sorted = (keys: string[]) => [...keys].sort();
  return (
    <Stack>
      {connections.length > 1 && (
        <Field label="Photo library" help="Which Immich this Source reads.">
          <Select
            value={value.connection_ref}
            options={connections.map((c) => ({ value: c.connection_ref, label: c.name }))}
            onChange={(connection_ref) => onChange({ ...defaultQuery(connection_ref), exclude: { ...defaultQuery(connection_ref).exclude } })}
          />
        </Field>
      )}
      <Section title="Include">
        <Stack>
          <ItemPicker label="Albums" help="Photos in every chosen album." search={lookups.albums.search} byId={lookups.albums.byId}
            value={value.albums} onChange={(keys) => set({ albums: sorted(keys) })} keyOf={albumKey} render={albumWords}
            chosenNote={ALL_OF_THEM} />
          <ItemPicker label="People" help="Photos every chosen person is in." search={lookups.people.search} byId={lookups.people.byId}
            value={value.people} onChange={(keys) => set({ people: sorted(keys) })} keyOf={personKey} render={personWords}
            chosenNote={ALL_OF_THEM} />
          <ItemPicker label="Place" help="A city, its state and its country, as Immich names them." search={lookups.places.search}
            value={value.place === null ? [] : [placeKey(value.place)]}
            onChange={(_keys, chosen) => set({ place: chosen[0] ?? null })}
            keyOf={placeKey} render={placeWords} max={1} />
          <Field label="Dates" help="Dates in this browser's time zone; In the last counts back from each check.">
            <SegmentedControl<TakenChoice>
              label="Dates"
              value={choice}
              options={[{ value: "any", label: "Any time" }, { value: "between", label: "Between" }, { value: "last", label: "In the last" }]}
              onChange={chooseTaken}
            />
          </Field>
          {taken?.kind === "between" && (
            <Inline>
              <Field label="From" error={datesProblem ?? undefined}>
                <TextInput type="date" value={dayText(taken.start)}
                  onChange={(text) => set({ taken: { ...taken, start: dayStart(text) } })} />
              </Field>
              <Field label="To">
                <TextInput type="date" value={dayText(taken.end === null ? null : taken.end - 86400)}
                  onChange={(text) => set({ taken: { ...taken, end: dayStart(text, 1) } })} />
              </Field>
            </Inline>
          )}
          {taken?.kind === "last" && (
            <Inline>
              <Field label="Years">
                <NumberInput value={taken.years} min={0} max={200} step={1}
                  onChange={(text) => set({ taken: { ...taken, years: Math.max(0, Math.trunc(Number(text) || 0)) } })} />
              </Field>
              <Field label="Months">
                <NumberInput value={taken.months} min={0} max={11} step={1}
                  onChange={(text) => set({ taken: { ...taken, months: Math.min(11, Math.max(0, Math.trunc(Number(text) || 0))) } })} />
              </Field>
            </Inline>
          )}
          <Field label="Favourites only">
            <Switch checked={value.favourites_only} onChange={(on) => set({ favourites_only: on })} />
          </Field>
          <ItemPicker label="Tags" help="Photos with every chosen tag; a tag includes the tags inside it."
            search={lookups.tags.search} byId={lookups.tags.byId}
            value={value.tags} onChange={(keys) => set({ tags: sorted(keys) })} keyOf={tagKey} render={tagWords}
            chosenNote={ALL_OF_THEM} />
          <Inline label="Media types">
            {MEDIA.map(({ type, label }) => {
              const on = value.media_types.includes(type);
              return (
                <Field key={type} label={label}>
                  <Switch checked={on} disabled={on && value.media_types.length === 1} onChange={(next) => toggleMedia(type, next)} />
                </Field>
              );
            })}
          </Inline>
          <Field label="Include partners' photos" help="Photos your Immich partners share with you.">
            <Switch checked={value.partners} onChange={(on) => set({ partners: on })} />
          </Field>
        </Stack>
      </Section>
      <Section title="Exclude">
        <Stack>
          <ItemPicker label="Leave out albums" search={lookups.albums.search} byId={lookups.albums.byId}
            value={value.exclude.albums} onChange={(keys) => exclude({ albums: sorted(keys) })} keyOf={albumKey} render={albumWords} />
          <ItemPicker label="Leave out people" search={lookups.people.search} byId={lookups.people.byId}
            value={value.exclude.people} onChange={(keys) => exclude({ people: sorted(keys) })} keyOf={personKey} render={personWords} />
          <ItemPicker label="Leave out tags" search={lookups.tags.search} byId={lookups.tags.byId}
            value={value.exclude.tags} onChange={(keys) => exclude({ tags: sorted(keys) })} keyOf={tagKey} render={tagWords} />
          <Field label="Leave out screenshots" help="A file named screenshot, or a PNG with no camera maker.">
            <Switch checked={value.exclude.screenshots} onChange={(on) => exclude({ screenshots: on })} />
          </Field>
          <Note>Archived, hidden and locked photos are never included.</Note>
        </Stack>
      </Section>
    </Stack>
  );
}
