import type { Meta, StoryObj } from "@storybook/react-vite";

import { type FitBoardState, FitBoard } from "./fit-board";
import { ALL_FIT, EMPTY, NONE_FIT, READING, READY } from "./fit-board.fixtures";

const STATES: Record<string, FitBoardState> = {
  loading: { status: "loading" },
  reading: { status: "ready", view: READING, updating: true },
  ready: { status: "ready", view: ALL_FIT, updating: false },
  partial: { status: "ready", view: READY, updating: false },
  noneFit: { status: "ready", view: NONE_FIT, updating: false },
  empty: { status: "ready", view: EMPTY, updating: false },
  cantTell: { status: "cant-tell", view: READY,
    what: "Central did not answer. These are the photos as read at Sun 03:39.", onRetry: () => {} },
  error: { status: "error", what: "Central did not answer.", onRetry: () => {} },
};

const meta = {
  title: "Patterns/FitBoard",
  component: FitBoard,
  args: { state: STATES.ready, size: "full", onShuffle: () => {}, onOpen: () => {},
    heading: "Which Frames can show them", collapse: false },
} satisfies Meta<typeof FitBoard>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Every Frame has photos that fit: each drawn with what it would show. */
export const Ready: Story = {};
/** One Frame has nothing that fits: warned of on top, in the Planner's words. */
export const Partial: Story = { args: { state: STATES.partial } };
/** The only Frame has nothing that fits. */
export const NoneFit: Story = { args: { state: STATES.noneFit } };
/** Immich is still being read: "so far", and a newer answer on its way (dimmed under a line). */
export const Reading: Story = { args: { state: STATES.reading } };
/** The first read. */
export const Loading: Story = { args: { state: STATES.loading } };
/** Nothing in the selection. */
export const Empty: Story = { args: { state: STATES.empty } };
/** A read failed after an answer: the last answer, greyed, as Can't tell. */
export const CantTell: Story = { args: { state: STATES.cantTell } };
/** The first read failed. */
export const Error: Story = { args: { state: STATES.error } };

export const CompactReady: Story = { args: { size: "compact", state: STATES.ready } };
export const CompactPartial: Story = { args: { size: "compact", state: STATES.partial } };
export const CompactNoneFit: Story = { args: { size: "compact", state: STATES.noneFit } };
export const CompactReading: Story = { args: { size: "compact", state: STATES.reading } };
export const CompactLoading: Story = { args: { size: "compact", state: STATES.loading } };
export const CompactEmpty: Story = { args: { size: "compact", state: STATES.empty } };
export const CompactCantTell: Story = { args: { size: "compact", state: STATES.cantTell } };
export const CompactError: Story = { args: { size: "compact", state: STATES.error } };

/** One line that opens the full board in a sheet (Show now, a Source's card, a phone). */
export const LineReady: Story = { args: { size: "line", state: STATES.ready } };
export const LinePartial: Story = { args: { size: "line", state: STATES.partial } };
export const LineNoneFit: Story = { args: { size: "line", state: STATES.noneFit } };
export const LineReading: Story = { args: { size: "line", state: STATES.reading } };
export const LineLoading: Story = { args: { size: "line", state: STATES.loading } };
export const LineEmpty: Story = { args: { size: "line", state: STATES.empty } };
export const LineCantTell: Story = { args: { size: "line", state: STATES.cantTell } };
export const LineError: Story = { args: { size: "line", state: STATES.error } };
