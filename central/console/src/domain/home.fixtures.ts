import type { FramePlace, FrameStatus, HomeView, Override, Problem } from "../generated/contract";

/* Home's document in a few states, for the catalogue: a living room wall and a hall, at 21:10 on the
   house clock (Europe/London, a winter evening, so house time is UTC). Every time is Central's. */

const at = (hour: number, minute: number, day = 22) => Date.UTC(2026, 10, day, hour, minute) / 1000;

/** Central's time of the read. */
export const NOW = at(21, 10);

export const PIS = { "device-a": "Pi 3f2a9c", "device-b": "Pi 77c0d1" } as const;

const EVENINGS = { kind: "program" as const, name: "Evenings", scene_id: "evenings", program_id: "evenings", until: at(23, 0) };

function status(frameId: string, over: Partial<FrameStatus> = {}): FrameStatus {
  return { frame_id: frameId, word: "showing", setup: null, since: at(17, 0), because: { row: "showing" }, can: [], ...over };
}

function place(frameId: string, name: string, x: number, y: number, size: [number, number] | null, device: string | null,
  over: Partial<FrameStatus> = {}, playing: FramePlace["playing"] = EVENINGS): FramePlace {
  return { frame_id: frameId, name, x_mm: x, y_mm: y, size_mm: size, device_id: device,
    player_id: device === null ? null : `player-${device}`, playing, status: status(frameId, over) };
}

const RESTING: Partial<FrameStatus> = { word: "resting", since: at(21, 0), because: { row: "resting", power: "night-off", until: at(7, 0, 23) } };

function home(frames: { living: FramePlace[]; hall: FramePlace[] }, word: HomeView["house"]["word"], problems: Problem[] = [],
  overrides: Override[] = []): HomeView {
  return {
    change: 1,
    house: { word, problems },
    walls: [
      { surface_id: "living", name: "Living room wall", room: "Living room", frames: frames.living },
      { surface_id: "hall", name: "Hall", room: "Upstairs", frames: frames.hall },
    ],
    overrides,
    now_next: [
      { surface_id: "living", now: EVENINGS, next: { program_id: "mornings", scene_id: "mornings", name: "Mornings", at: at(7, 0, 23) } },
      { surface_id: "hall", now: { kind: "default-scene", name: "family", scene_id: "family" }, next: null },
    ],
  };
}

const LIVING = [
  place("living-left", "Living room left", 0, 0, [1100, 620], "device-a"),
  place("window", "By the window", 1200, 0, [520, 900], "device-a"),
  place("door", "By the door", 1800, 0, [520, 900], "device-b"),
];
const HALL = [
  place("hall-left", "Hall left", 0, 0, [600, 400], "device-b", RESTING, null),
  place("hall-right", "Hall right", 700, 0, [600, 400], "device-b", RESTING, null),
];

/** Every Frame showing or resting. */
export const ALL_GOOD = home({ living: LIVING, hall: HALL }, "all-good");

const APP_GONE: Problem = {
  part: "photo app", code: "app-absent", since: at(21, 4), doing: null, frame_id: "door",
  fix: { kind: "restart-app", device_id: "device-b" },
  evidence: { layer: "health-verdict", detail: "app_absent", at: at(21, 4), pi_time: "2026-11-22T21:04:02Z" },
};
const PI_GONE: Problem = {
  part: "pi", code: "pi-not-linked", since: at(21, 4), doing: "reconnecting", frame_id: "window",
  fix: { kind: "open", page: "pi", id: "device-a" }, evidence: { layer: "presence", at: at(21, 4) },
};
const LIBRARY: Problem = {
  part: "immich", code: "library-needs-a-look", since: at(3, 39), doing: "keeps-playing", frame_id: "living-left",
  fix: { kind: "check-now", connection_ref: "immich" }, evidence: { layer: "library", detail: "key_rejected", at: at(3, 39) },
};

/** One Frame of each word, a Frame in setup, and their problems worst first. */
export const PROBLEMS = home({
  living: [
    place("living-left", "Living room left", 0, 0, [1100, 620], "device-a",
      { word: "needs-a-look", since: at(3, 39), because: { row: "showing-with-trouble" }, problem: LIBRARY }),
    place("window", "By the window", 1200, 0, [520, 900], "device-a",
      { word: "cant-tell", since: at(21, 4), because: { row: "pi-not-heard" }, problem: PI_GONE, can: ["reboot-pi"] }, null),
    place("door", "By the door", 1800, 0, [520, 900], "device-b",
      { word: "not-showing", since: at(21, 4), because: { row: "dark" }, problem: APP_GONE, can: ["reboot-pi", "restart-app"] }),
  ],
  hall: [
    place("hall-left", "Hall left", 0, 0, [600, 400], "device-b",
      { word: "getting-ready", since: at(21, 8), because: { row: "dark" }, progress: { step: "preparing-photos", done: 3, total: 12 } }),
    place("hall-right", "Hall right", 700, 0, [600, 400], null,
      { word: null, setup: "needs-a-pi", because: { row: "setup" } }, null),
  ],
}, "not-showing", [APP_GONE, PI_GONE, LIBRARY]);

const HUB: Problem = {
  part: "central", code: "hub-unreachable", since: at(20, 58), doing: "reconnecting", frame_id: null,
  fix: { kind: "open", page: "central", id: null }, evidence: { layer: "hub", detail: "unreachable", at: at(20, 58) },
  explains: ["living-left", "window", "door", "hall-left", "hall-right"],
};
const cantHear = (frame: FramePlace): FramePlace => ({ ...frame, playing: null,
  status: status(frame.frame_id, { word: "cant-tell", since: at(20, 58), because: { row: "central-cant-hear" } }) });

/** Central can't hear any Pi: one house problem, not one card a Frame. */
export const HOUSE_PROBLEM = home({ living: LIVING.map(cantHear), hall: HALL.map(cantHear) }, "cant-tell", [HUB]);

const HOLD: Override = { kind: "power-hold", id: "hold-1", frame_ids: ["hall-left", "hall-right"], power: "off", by: null,
  started_at: at(20, 0), until: at(23, 0) };
const held = (frame: FramePlace): FramePlace => ({ ...frame,
  status: status(frame.frame_id, { word: "resting", since: at(20, 0), because: { row: "resting", power: "hold", until: at(23, 0) }, override: HOLD }) });

/** All off on the hall until 23:00: the hold with its way back. */
export const HELD = home({ living: LIVING, hall: HALL.map(held) }, "all-good", [], [HOLD]);

/** A failure during intended darkness (finding F1): Needs a look, and when it won't show. */
export const WONT_SHOW = home({
  living: LIVING,
  hall: [
    place("hall-left", "Hall left", 0, 0, [600, 400], "device-b", {
      word: "needs-a-look", since: at(21, 4), because: { row: "wont-show", power: "night-off", until: at(7, 0, 23) },
      problem: { ...APP_GONE, frame_id: "hall-left" }, can: ["restart-app"],
    }, null),
    HALL[1],
  ],
}, "needs-a-look", [{ ...APP_GONE, frame_id: "hall-left" }]);

/** No Frame yet. */
export const EMPTY: HomeView = { change: 0, house: { word: "all-good", problems: [] }, walls: [], overrides: [], now_next: [] };

const EVERYDAY_CAN: FrameStatus["can"] = ["never-show", "pause", "previous", "reboot-pi", "restart-app", "skip"];
const movable = (frame: FramePlace, over: Partial<FrameStatus> = {}): FramePlace => ({ ...frame,
  status: { ...frame.status, can: EVERYDAY_CAN, photo: `photo-${frame.frame_id}`, ...over } });
const withWallCan = (view: HomeView): HomeView => ({ ...view,
  walls: view.walls.map((wall) => (wall.surface_id === "living" ? { ...wall, can: ["previous", "skip"] } : wall)) });

/** Slice 3: every living room Frame can move, so the wall offers Skip and Previous and each tile's
 * menu its everyday actions. */
export const EVERYDAY = withWallCan(home({ living: LIVING.map((frame) => movable(frame)), hall: HALL }, "all-good"));

/** A wall's Skip just pressed: two Frames wait for their Pi to show the new photo; one is late. */
export const SKIP_REQUESTED = withWallCan(home({
  living: [
    movable(LIVING[0], { asked: { action: "skip", request_id: "r1", at: at(21, 10), state: "requested" } }),
    movable(LIVING[1], { asked: { action: "skip", request_id: "r1", at: at(21, 10), state: "requested" } }),
    movable(LIVING[2], { asked: { action: "skip", request_id: "r0", at: at(21, 9), state: "late" } }),
  ],
  hall: HALL,
}, "all-good"));

const TAKEOVER: Override = { kind: "takeover", id: "run-holiday", frame_ids: ["living-left", "window"], name: "Holiday 2024",
  started_at: at(20, 30), until: at(22, 0) };
const PAUSE: Override = { kind: "pause", id: "pause-1", frame_ids: ["door"], by: "Matt", started_at: at(21, 4), until: at(22, 4) };
const HOLIDAY = { kind: "show-now" as const, name: "Holiday 2024", scene_id: "holiday", until: at(22, 0) };

/** Show now on two Frames until 22:00, and a pause on the third: each banner with its way back. */
export const TAKEOVER_AND_PAUSE = withWallCan(home({
  living: [
    { ...movable(LIVING[0], { override: TAKEOVER }), playing: HOLIDAY },
    { ...movable(LIVING[1], { override: TAKEOVER }), playing: HOLIDAY },
    movable(LIVING[2], { override: PAUSE, can: ["never-show", "reboot-pi", "restart-app"],
      asked: { action: "pause", request_id: "pause-1", at: at(21, 4), state: "done" } }),
  ],
  hall: HALL,
}, "all-good", [], [TAKEOVER, PAUSE]));
