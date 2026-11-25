import type { Meta, StoryObj } from "@storybook/react-vite";

import { WallActions } from "./wall-actions";
import { NowNextStrip, WallMap, type WallMapFrame } from "./wall-map";
import { WallTile } from "./wall-tile";

const tile = (name: string, label = "Showing", severity: "ok" | "alarm" | "notice" | "unknown" | "todo" = "ok", line: string | null = "Evenings · until 23:00") => (
  <WallTile name={name} href="#/wall/frames/sample/overview" chip={{ severity, label }} line={line}
    menu={{ label: `More actions for ${name}`, as: "popup", items: [{ label: "Open Frame", href: "#/wall/frames/sample/overview" }] }} />
);

/** A living room wall: a large landscape Frame, two portrait ones beside it, one small one under them. */
const LIVING: WallMapFrame[] = [
  { key: "left", x: 0, y: 0, size: [1100, 620], tile: tile("Living room left") },
  { key: "tall-1", x: 1200, y: 0, size: [520, 900], tile: tile("By the window", "Resting", "ok", "Night off until 07:00") },
  { key: "tall-2", x: 1800, y: 0, size: [520, 900], tile: tile("By the door", "Not showing", "alarm", "The photo app is not running on the Pi.") },
  { key: "small", x: 0, y: 700, size: [700, 400], tile: tile("Under the shelf", "Getting ready", "notice", "Preparing photos · 3 of 12") },
];

const meta = {
  title: "Patterns/WallMap",
  component: WallMap,
  args: {
    name: "Living room wall",
    room: "Living room",
    frames: LIVING,
    below: <NowNextStrip now="Evenings · until 23:00" next="Mornings · 07:00" />,
  },
} satisfies Meta<typeof WallMap>;

export default meta;
type Story = StoryObj<typeof meta>;

/** One wall, its Frames to scale, with now and next under it. */
export const OneWall: Story = {};
/** Two walls, one under the other, each with its own strip. */
export const TwoWalls: Story = {
  render: (args) => (
    <div className="flex flex-col gap-6">
      <WallMap name={args.name} room={args.room} frames={args.frames} below={args.below} />
      <WallMap name="Hall" room="Upstairs" below={<NowNextStrip now="Default Scene: family" next={null} />}
        frames={[
          { key: "a", x: 0, y: 0, size: [600, 400], tile: tile("Hall left", "Showing", "ok", "Default Scene: family") },
          { key: "b", x: 700, y: 0, size: [600, 400], tile: tile("Hall right", "Can't tell", "unknown", "Can't reach Pi 3f2a9c since 21:04.") },
        ]} />
    </div>
  ),
};
/** A wall whose Frames can move: Skip and Previous for the whole wall beside its name. */
export const WithActions: Story = {
  args: {
    actions: (
      <WallActions wall="Living room wall" actions={[
        { key: "next", label: "Skip", name: "Skip every Frame on Living room wall", onAction: () => {} },
        { key: "previous", label: "Previous", name: "Previous on every Frame on Living room wall", onAction: () => {} },
      ]} />
    ),
  },
};
/** A wall with no Frame yet: its name, and nothing under it. */
export const Empty: Story = { args: { frames: [], below: undefined } };
/** A Frame whose size is not known: the wall is not drawn to scale, and says so. */
export const NotToScale: Story = {
  args: { frames: [...LIVING.slice(0, 2), { key: "new", x: 0, y: 0, size: null, tile: tile("New Frame", "Needs a Pi", "todo", "Choose which Pi and HDMI port feeds this Frame") }] },
};
/** The first read is on its way: skeleton tiles in the Frames' places. */
export const Loading: Story = { args: { loading: true } };
/** At phone width: the same tiles two across; nothing scrolls sideways. */
export const Phone: Story = { parameters: { phone: true } };
