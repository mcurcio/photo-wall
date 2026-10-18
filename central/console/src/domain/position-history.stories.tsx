import type { Meta, StoryObj } from "@storybook/react-vite";

import type { Adjustment } from "./live-adjustment";
import { type PositionEntryView, type PositionHistoryView, PositionHistoryList } from "./position-history";
import { LIVING_ROOM, PROFILES, ROOMS } from "./room-lights/room-lights.fixtures";

const FRAME = { id: "lobby", name: "Lobby" };
type Rectangle = { centre_x: number; centre_y: number; width: number };
const OPENING: Rectangle = { centre_x: 0.5, centre_y: 0.49, width: 0.82 };
const FULL: Rectangle = { centre_x: 0.5, centre_y: 0.5, width: 1 };
const NAMES = Object.fromEntries(ROOMS.lights.map((light) => [light.id, light.light.name]));

const ADJUSTMENT: Adjustment = {
  draft: { fit: { mode: "rectangle", ...OPENING }, lighting: LIVING_ROOM },
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
  rectangle?: Rectangle; depth?: number;
  lights?: PositionEntryView["lighting"]["lights"]; profile?: { width: number; height: number };
  refusal?: PositionEntryView["restore_refusal"];
} = {}): PositionEntryView {
  const fit: PositionEntryView["fit"] = { mode: "rectangle", ...(values.rectangle ?? OPENING) };
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
    entry("k1", "kept-saved", "2026-10-12T21:10:02Z", { rectangle: FULL, depth: 0, lights: [] }),
    entry("k0", "kept-created", "2026-10-12T21:02:40Z", { rectangle: FULL, depth: 0, lights: LIVING_ROOM.lights.slice(1, 2) }),
  ],
};

const meta = {
  title: "Domain/PositionHistory",
  component: PositionHistoryList,
  args: {
    history: KEPT, adjustment: ADJUSTMENT, roomNames: NAMES, profiles: PROFILES, busy: false, restoring: null,
    onRestore: () => {}, onRetry: () => {},
  },
} satisfies Meta<typeof PositionHistoryList>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Kept Positions over two days: the picture a little smaller than the screen with a 2.5 mm mat, daylight and the overheads, the full screen
 * before, and the Frame as it was created. */
export const Kept: Story = {};
/** A visit: the picture moved and the mat deepened, tried and not kept (dashed), then put back by Revert. */
export const NotKept: Story = {
  args: {
    history: {
      ...KEPT,
      entries: [
        entry("s3", "sent-back", "2026-10-13T20:04:40Z"),
        entry("s2", "sent-draft", "2026-10-13T20:04:07Z", { depth: 6 }),
        entry("s1", "sent-draft", "2026-10-13T20:03:55Z", { rectangle: { centre_x: 0.52, centre_y: 0.49, width: 0.82 } }),
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
