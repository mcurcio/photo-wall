import type { Meta, StoryObj } from "@storybook/react-vite";

import { SourcePoolView } from "./source-pool-view";
import { AT, CHECKING, COMPLETE, poolItems } from "./source.fixtures";

const meta = {
  title: "Domain/SourcePoolView",
  component: SourcePoolView,
  args: { view: CHECKING, pages: [CHECKING.items], hasMore: true, onLoadMore: () => {} },
} satisfies Meta<typeof SourcePoolView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Photos join as Immich is read: "12,400 in the pool so far · up to 41,203 in Immich". */
export const Checking: Story = {};

export const Complete: Story = { args: { view: COMPLETE, pages: [COMPLETE.items], hasMore: false } };

export const Incomplete: Story = {
  args: {
    view: { ...COMPLETE, check: { state: "incomplete", reason: "over_1000_at_one_instant", last_complete_at: AT - 900 } },
    pages: [COMPLETE.items],
    hasMore: false,
  },
};

/** Immich could not be read: the last pool keeps playing. */
export const FailedConnection: Story = {
  args: {
    view: { ...COMPLETE, check: { state: "failed", problem: "connection", last_complete_at: AT - 900 } },
    pages: [poolItems(6)],
    hasMore: false,
  },
};

export const ChosenItemGone: Story = {
  args: {
    view: { ...COMPLETE, check: { state: "failed", problem: "chosen_item_gone", last_complete_at: null } },
    pages: [poolItems(6)],
    hasMore: false,
  },
};

/** A complete check that found nothing. */
export const Empty: Story = {
  args: { view: { ...COMPLETE, in_pool: { photos: 0, videos: 0 }, items: [] }, pages: [[]], hasMore: false },
};

/** Nothing matches: the query's choices are named, each with a Clear. */
export const EmptyWithChoices: Story = {
  args: {
    view: { ...COMPLETE, in_pool: { photos: 0, videos: 0 }, items: [] },
    pages: [[]],
    hasMore: false,
    filters: [
      { label: "People: 2 chosen", onClear: () => {} },
      { label: "Dates: last 3 years", onClear: () => {} },
    ],
  },
};

export const NotCheckedYet: Story = {
  args: { view: { ...COMPLETE, check: { state: "never_checked" }, in_pool: { photos: 0, videos: 0 }, items: [] }, pages: [[]], hasMore: false },
};

export const Reading: Story = { args: { view: null, pages: [] } };

/** A newer read of the same photos is running beside it (the Fit board's "so far"): the grid is
 * dimmed and its own count hidden, so the page shows one count. */
export const OlderWhileReading: Story = { args: { view: COMPLETE, pages: [COMPLETE.items], hasMore: false, reading: true } };
