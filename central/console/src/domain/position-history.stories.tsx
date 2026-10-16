import type { Meta, StoryObj } from "@storybook/react-vite";

import type { Adjustment } from "./live-adjustment";
import { type PositionEntryView, type PositionHistoryView, PositionHistoryList } from "./position-history";
import { LIVING_ROOM, PROFILES, ROOMS } from "./room-lights/room-lights.fixtures";

const FRAME = { id: "lobby", name: "Lobby" };
type Corners = [[number, number], [number, number], [number, number], [number, number]];
const OPENING: Corners = [[0.1, 0.08], [0.9, 0.05], [0.92, 0.95], [0.08, 0.92]];
const FULL: Corners = [[0, 0], [1, 0], [1, 1], [0, 1]];
const NAMES = Object.fromEntries(ROOMS.lights.map((light) => [light.id, light.light.name]));

const ADJUSTMENT: Adjustment = {
  draft: { fit: { mode: "cover", corners: OPENING, rotation: 0 }, lighting: LIVING_ROOM },
  change: () => ({ valid: true }),
  changeLighting: () => {},
  dirty: false,
  phase: "shown",
  error: null,
  busy: false,
  ack: { revision: 1, at: 1_791_000_000 },
  latest: 1,
  done: async () => true,
  revert: async () => {},
  retry: () => {},
  restore: async () => ({ ok: true }),
  savedAt: null,
  leftAt: null,
};

// Times in UTC: the story's times show in the browser's zone (no house clock here).
function entry(id: string, kind: PositionEntryView["kind"], at: string, values: {
  corners?: Corners; rotation?: number; mode?: "cover" | "corners"; depth?: number;
  lights?: PositionEntryView["lighting"]["lights"]; profile?: { width: number; height: number };
  refusal?: PositionEntryView["restore_refusal"];
} = {}): PositionEntryView {
  const corners = values.corners ?? OPENING;
  const fit: PositionEntryView["fit"] = values.mode === "corners"
    ? { mode: "corners", corners, rotation: (values.rotation ?? 0) as 0 | 90 | 180 | 270 }
    : { mode: "cover", corners, rotation: values.rotation ?? 0 };
  return {
    entry_id: id, frame: FRAME, kind, number: 1, at, session_id: null, restored_from: null, fit,
    lighting: { ...LIVING_ROOM, mat_depth_mm: values.depth ?? 2.5, lights: values.lights ?? LIVING_ROOM.lights.slice(0, 2) },
    made_for: { profile_px: values.profile ?? { width: 1920, height: 1080 }, display_id: null },
    restore_refusal: values.refusal ?? null,
  };
}

const KEPT: PositionHistoryView = {
  frame: FRAME,
  unrecorded_change: false,
  entries: [
    entry("k2", "kept-saved", "2026-10-13T19:58:31Z"),
    entry("k1", "kept-saved", "2026-10-12T21:10:02Z", { corners: FULL, depth: 0, lights: [] }),
    entry("k0", "kept-created", "2026-10-12T21:02:40Z", { corners: FULL, depth: 0, lights: LIVING_ROOM.lights.slice(1, 2) }),
  ],
};

const meta = {
  title: "Domain/PositionHistory",
  component: PositionHistoryList,
  args: {
    history: KEPT, adjustment: ADJUSTMENT, size: { width: 1920, height: 1080 }, roomNames: NAMES, profiles: PROFILES, busy: false, restoring: null,
    onRestore: () => {}, onRetry: () => {},
  },
} satisfies Meta<typeof PositionHistoryList>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Kept Positions over two days: Cover in an opening with a 2.5 mm mat, daylight and the overheads, the full screen
 * before, and the Frame as it was created. */
export const Kept: Story = {};
/** A visit: corners moved and the mat deepened, tried and not kept (dashed), then put back by Revert. */
export const NotKept: Story = {
  args: {
    history: {
      ...KEPT,
      entries: [
        entry("s3", "sent-back", "2026-10-13T20:04:40Z"),
        entry("s2", "sent-draft", "2026-10-13T20:04:07Z", { depth: 6 }),
        entry("s1", "sent-draft", "2026-10-13T20:03:55Z", { corners: [[0.12, 0.08], [0.9, 0.05], [0.92, 0.95], [0.08, 0.92]] }),
        ...KEPT.entries,
      ],
    },
  },
};
/** Positions made for another screen size, before the Frame's profile changed: Restore says why it is refused. */
export const OtherProfile: Story = {
  args: {
    history: {
      ...KEPT,
      entries: [
        entry("p1", "kept-profile-changed", "2026-10-13T20:30:00Z", { profile: { width: 3840, height: 2160 } }),
        ...KEPT.entries.map((kept) => ({ ...kept, restore_refusal: "made_for_other_profile" as const })),
      ],
    },
  },
};
/** A Position made on another display than the one on the Frame's port now. */
export const OtherDisplay: Story = {
  args: {
    history: {
      ...KEPT,
      unrecorded_change: true,
      entries: KEPT.entries.map((kept, index) => (index === 0 ? kept : { ...kept, restore_refusal: "made_for_other_display" as const })),
    },
  },
};
/** No editor running (someone else is adjusting this Frame): Restore waits. */
export const NoSession: Story = { args: { adjustment: { ...ADJUSTMENT, phase: "waiting" } } };
