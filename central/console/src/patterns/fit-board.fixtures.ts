import type { CropView } from "./crop-thumb";
import type { FitBoardView } from "./fit-board";
import type { FitAlert, FrameFitView } from "./frame-fit-row";
import type { ShapeBin } from "./shape-map";

/*
 * The Fit board's catalog data: the varied library's shapes (3:2, 4:3, 16:9, 9:16, 1:1, a 4:1
 * panorama) on three Frames (16:9, 9:16, 4:3), worded as photo-fit.ts words them. Pictures are
 * inline SVG: a gradient with a bright disc where the subject sits, so a crop reads.
 */

function picture(width: number, height: number, shade: number, subject: [number, number] = [0.5, 0.5]): string {
  const r = Math.min(width, height) / 4;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">`
    + `<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="rgb(${shade},${shade + 30},${shade + 70})"/>`
    + `<stop offset="1" stop-color="rgb(${shade + 90},${shade + 60},${shade + 20})"/></linearGradient></defs>`
    + `<rect width="${width}" height="${height}" fill="url(#g)"/>`
    + `<circle cx="${width * subject[0]}" cy="${height * subject[1]}" r="${r}" fill="rgb(238,214,160)"/></svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

/** A photo `w`:`h` cropped for a Frame of `aspect` at `offset`. */
export function crop(key: string, w: number, h: number, aspect: number, offset = 0.5, verdict = "fits",
  refused = false, subject: [number, number] = [0.5, 0.5]): CropView {
  return {
    key, src: picture(w * 40, h * 40, 40 + (key.length * 17) % 80, subject), alt: `Photo, ${w}:${h}`,
    width: w * 1000, height: h * 1000, aspect, offset, verdict, refused,
  };
}

/** hall (9:16) with nothing that fits, worded as photo-fit.ts words it. */
export const NOTHING_FITS: FitAlert = {
  severity: "alarm", label: "Nothing fits",
  what: "hall is 9:16 and these photos are 4:3: add photos of that shape or raise the crop limit.",
};

/** stairs with a single photo, shared with other Frames. */
export const FEW_PHOTOS: FitAlert = {
  severity: "notice", label: "Few photos",
  what: "Only 1 photo fits stairs, and a photo never shows on two Frames at once, so stairs will often keep its last photo.",
};

const WIDE = 16 / 9;
const TALL = 9 / 16;
const FOUR_THREE = 4 / 3;

export const LIVING: FrameFitView = {
  key: "living-room", name: "living-room", aspect: WIDE, ratio: "16:9",
  lead: crop("l0", 3, 2, WIDE, 0.4),
  emptyText: "Not checked yet",
  count: "1,520 of 1,689 fit",
  segments: [
    { key: "fit", label: "fit", count: 1520, countText: "1,520" },
    { key: "beyond_crop_limit", label: "doesn't fit this Frame's shape", count: 158, countText: "158" },
    { key: "below_frame_pixels", label: "too small for this Frame", count: 11, countText: "11" },
  ],
  samples: [crop("l1", 3, 2, WIDE, 0.5), crop("l2", 4, 3, WIDE, 0.3, "fits", false, [0.5, 0.3]),
    crop("l3", 16, 9, WIDE), crop("l4", 3, 2, WIDE, 0.7), crop("l5", 4, 3, WIDE, 0.5), crop("l6", 3, 2, WIDE, 0.2)],
  checked: "Faces and the interesting part: checked 64 of 1,520; 61 keep them whole.",
  detail: "Frame output 1,512 × 851 px, 16:9 · on a 3,840 × 2,160 display",
  alert: null,
  why: {
    summary: "Why 169 don't fit",
    reasons: [
      { key: "beyond_crop_limit", label: "doesn't fit this Frame's shape", countText: "158",
        samples: [crop("r1", 9, 16, WIDE, 0.5, "doesn't fit this Frame's shape", true), crop("r2", 1, 1, WIDE, 0.5, "doesn't fit this Frame's shape", true)] },
      { key: "below_frame_pixels", label: "too small for this Frame", countText: "11", samples: [crop("r3", 4, 3, WIDE, 0.5, "too small for this Frame", true)] },
      { key: "face_cut", label: "a face would be cut (of those checked)", countText: "3",
        samples: [crop("r4", 4, 3, WIDE, 0.5, "a face would be cut", true, [0.5, 0.1])] },
    ],
  },
  draft: null,
};

export const HALL: FrameFitView = {
  key: "hall", name: "hall", aspect: TALL, ratio: "9:16",
  lead: null,
  emptyText: "",
  count: "0 of 1,689 fit",
  segments: [
    { key: "fit", label: "fit", count: 0, countText: "0" },
    { key: "beyond_crop_limit", label: "doesn't fit this Frame's shape", count: 1689, countText: "1,689" },
  ],
  samples: [],
  checked: null,
  detail: "Frame output 1,080 × 1,920 px, 9:16 · on a 1,080 × 1,920 display",
  alert: NOTHING_FITS,
  why: {
    summary: "Why 1,689 don't fit",
    reasons: [{ key: "beyond_crop_limit", label: "doesn't fit this Frame's shape", countText: "1,689",
      samples: [crop("h1", 3, 2, TALL, 0.5, "doesn't fit this Frame's shape", true), crop("h2", 4, 3, TALL, 0.5, "doesn't fit this Frame's shape", true)] }],
  },
  draft: null,
};

export const OFFICE: FrameFitView = {
  key: "office", name: "office", aspect: FOUR_THREE, ratio: "4:3",
  lead: crop("o0", 4, 3, FOUR_THREE),
  emptyText: "Not checked yet",
  count: "1,601 of 1,689 fit",
  segments: [
    { key: "fit", label: "fit", count: 1601, countText: "1,601" },
    { key: "beyond_crop_limit", label: "doesn't fit this Frame's shape", count: 88, countText: "88" },
  ],
  samples: [crop("o1", 4, 3, FOUR_THREE), crop("o2", 3, 2, FOUR_THREE, 0.6), crop("o3", 1, 1, FOUR_THREE, 0.4)],
  checked: null,
  detail: "Size not known yet: office's screen hasn't reported. Judged by shape only.",
  alert: null,
  why: { summary: "Why 88 don't fit", reasons: [{ key: "beyond_crop_limit", label: "doesn't fit this Frame's shape", countText: "88", samples: [] }] },
  draft: null,
};

const COUNTS: Record<number, number> = { 9: 85, 13: 40, 16: 22, 20: 640, 22: 900, 24: 2 };

/** 33 log-spaced bins from 1:4 to 4:1, the varied library's photos in them. */
export const BINS: ShapeBin[] = Array.from({ length: 33 }, (_, index) => {
  const low = 0.25 * Math.exp((Math.log(16) * index) / 33);
  const high = 0.25 * Math.exp((Math.log(16) * (index + 1)) / 33);
  const total = COUNTS[index] ?? 0;
  return { low, high, total, label: `${low.toFixed(2)}–${high.toFixed(2)}`, count: `${total} photos`,
    frames: index >= 18 && index <= 26 ? "living-room, office" : "none" };
});

export const TICKS = ["1:4", "9:16", "3:4", "1:1", "4:3", "16:9", "4:1"].map((label, index) => ({
  label, ratio: [0.25, 0.5625, 0.75, 1, 4 / 3, 16 / 9, 4][index],
}));

export const BANDS = [
  { key: "living-room", name: "living-room", low: WIDE * 0.65, high: WIDE / 0.65 },
  { key: "hall", name: "hall", low: TALL * 0.65, high: TALL / 0.65 },
  { key: "office", name: "office", low: FOUR_THREE * 0.65, high: FOUR_THREE / 0.65 },
];

export const READY: FitBoardView = {
  summary: "1,689 photos · 42 videos",
  line: "Fits 2 of 3 Frames · hall has no photo that fits",
  judged: "Judged on every Frame at the house crop limit, 35 %: a Source has no Frames of its own.",
  reading: null,
  frames: [HALL, LIVING, OFFICE],
  shape: { bins: BINS, bands: BANDS, range: [0.25, 4], ticks: TICKS },
  empty: null,
};

/** A Frame with a single photo that fits: it fits, but too few to change. */
export const STAIRS: FrameFitView = {
  key: "stairs", name: "stairs", aspect: 2 / 3, ratio: "2:3",
  lead: crop("s0", 2, 3, 2 / 3),
  emptyText: "Not checked yet",
  count: "1 of 1,689 fit",
  segments: [
    { key: "fit", label: "fit", count: 1, countText: "1" },
    { key: "beyond_crop_limit", label: "doesn't fit this Frame's shape", count: 1688, countText: "1,688" },
  ],
  samples: [crop("s0", 2, 3, 2 / 3)],
  checked: null,
  detail: "Frame output 960 × 1,440 px, 2:3 · on a 1,920 × 1,080 display",
  alert: FEW_PHOTOS,
  why: { summary: "Why 1,688 don't fit", reasons: [{ key: "beyond_crop_limit", label: "doesn't fit this Frame's shape",
    countText: "1,688", samples: [crop("s1", 3, 2, 2 / 3, 0.5, "doesn't fit this Frame's shape", true)] }] },
  draft: null,
};

/** A Scene on three Frames: one fills well, one has a single photo, one has none. */
export const THIN: FitBoardView = {
  ...READY,
  line: "Fits 2 of 3 Frames · hall has no photo that fits · stairs has only 1 photo",
  judged: "At the Scene's crop limit, 35 %.",
  frames: [LIVING, STAIRS, HALL],
  shape: { ...READY.shape, bands: [...BANDS.filter((band) => band.key !== "office"),
    { key: "stairs", name: "stairs", low: (2 / 3) * 0.65, high: (2 / 3) / 0.65 }] },
};

export const ALL_FIT: FitBoardView = {
  ...READY, line: "Fits 2 of 2 Frames", frames: [LIVING, OFFICE],
  shape: { ...READY.shape, bands: BANDS.filter((band) => band.key !== "hall") },
};

export const READING: FitBoardView = {
  ...READY, summary: "812 photos · 12 videos so far", reading: "Still reading Immich: these are the photos found so far.",
};

export const NONE_FIT: FitBoardView = {
  ...READY,
  line: "hall has no photo that fits",
  frames: [HALL],
  shape: { ...READY.shape, bands: BANDS.filter((band) => band.key === "hall") },
};

export const EMPTY: FitBoardView = { ...READY, empty: "No photos in this selection.", frames: [] };
