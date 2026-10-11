import type { PoolItem, SourcePoolViewData } from "./source-pool-view";
import { type LibraryLookups, type SourceQuery, defaultQuery } from "./source-query-editor";

/** A fixed Central time, so every story renders the same times. */
export const AT = 1791147840;

/** The varied library's shapes (3:2, 4:3, 16:9, 9:16, 1:1, a 4:1 panorama, 3:4), in turn. */
const SHAPES: readonly [number, number][] = [[6000, 4000], [4032, 3024], [3840, 2160], [2160, 3840], [3000, 3000], [8000, 2000], [3024, 4032]];

/** A same-document thumbnail at the photo's ratio (one shade and a circle), so a story reaches no server. */
function swatch(shade: number, width: number, height: number): string {
  const grey = 60 + ((shade * 37) % 140);
  const w = Math.round(width / 50);
  const h = Math.round(height / 50);
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}"><rect width="${w}" height="${h}" fill="rgb(${grey},${grey},${grey})"/>`
    + `<circle cx="${w / 2}" cy="${h / 2}" r="${Math.min(w, h) / 3}" fill="rgb(${grey + 50},${grey + 50},${grey + 50})"/></svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

/** `count` pool photos, newest first, every fifth a video, their shapes varied. */
export function poolItems(count: number, from = 0): PoolItem[] {
  return Array.from({ length: count }, (_, index) => {
    const n = from + index;
    const [width, height] = SHAPES[n % SHAPES.length];
    return {
      asset_id: `asset-${n.toString(16).padStart(64, "0")}`,
      kind: n % 5 === 4 ? "video" : "image",
      taken_at: AT - n * 86400,
      width,
      height,
      thumbnail: swatch(n, width, height),
    };
  });
}

export const CHECKING: SourcePoolViewData = {
  check: { state: "checking", seen_so_far: 12400, started_at: AT - 120, last_complete_at: null },
  in_pool: { photos: 12100, videos: 300 },
  waiting: 37,
  left_out: [{ reason: "unreadable_answer", count: 2 }, { reason: "over_item_limits", count: 1 }],
  immich_up_to: 41203,
  items: poolItems(12),
  next: "cursor",
  read_at: AT,
};

export const COMPLETE: SourcePoolViewData = {
  check: { state: "complete", at: AT - 300 },
  in_pool: { photos: 2301, videos: 14 },
  waiting: 0,
  left_out: [],
  immich_up_to: 2400,
  items: poolItems(12),
  next: null,
  read_at: AT,
};

const ALBUMS = [
  { album_ref: "11111111-1111-4111-8111-111111111111", name: "Garden", photos: 412 },
  { album_ref: "22222222-2222-4222-8222-222222222222", name: "Holidays 2024", photos: 1290 },
];
const PEOPLE = [
  { person_ref: "33333333-3333-4333-8333-333333333333", name: "Anna" },
  { person_ref: "44444444-4444-4444-8444-444444444444", name: "Ben" },
];

/** Lookups that answer at once from a small list, as a connection's lists would. */
export const LOOKUPS: LibraryLookups = {
  albums: {
    search: async (q) => ({ items: ALBUMS.filter((a) => a.name.toLowerCase().includes(q.toLowerCase())), note: null }),
    byId: async (ids) => ALBUMS.filter((a) => ids.includes(a.album_ref)),
  },
  people: {
    search: async (q) => ({ items: PEOPLE.filter((p) => p.name.toLowerCase().includes(q.toLowerCase())), note: null }),
    byId: async (ids) => PEOPLE.filter((p) => ids.includes(p.person_ref)),
  },
  tags: { search: async () => ({ items: [], note: null }), byId: async () => [] },
  places: { search: async () => ({ items: [{ city: "Paris", state: "Île-de-France", country: "France" }], note: null }) },
};

/** "Anna and Ben, last 3 years, no screenshots", in two Garden albums. */
export const ANNA_AND_BEN: SourceQuery = {
  ...defaultQuery("library-0123456789ab"),
  albums: ALBUMS.map((a) => a.album_ref),
  people: PEOPLE.map((p) => p.person_ref),
  taken: { kind: "last", years: 3, months: 0 },
};
