import type { Meta, StoryObj } from "@storybook/react-vite";

import {
  ALL_GOOD, EMPTY, EVERYDAY, HELD, HOUSE_PROBLEM, NOW, PIS, PROBLEMS, SKIP_REQUESTED, TAKEOVER_AND_PAUSE, WONT_SHOW,
} from "../domain/home.fixtures";
import { HomeBoard } from "../domain/home-board";
import { StepProgress } from "../patterns/step-progress";
import { setHouseClock } from "../timeWords.js";

const meta = {
  title: "Pages/Home",
  component: HomeBoard,
  tags: ["full-page"],
  args: {
    state: { kind: "ready", home: ALL_GOOD, staleSince: null },
    now: NOW, pis: PIS, requested: {}, busy: false, said: null, menuAs: "popup",
    onFix: () => {}, onAction: () => {}, onHold: () => {}, onResume: () => {},
    onEnd: () => {}, onWallAction: () => {}, onRetry: () => {},
  },
  // Every time on Home is Central's, said on the house clock.
  beforeEach: () => {
    setHouseClock({ zone: "Europe/London", clock: "24h" });
    return () => setHouseClock({ zone: null, clock: "24h" });
  },
} satisfies Meta<typeof HomeBoard>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Every Frame showing or resting: the house word, the walls, now and next, the quick actions. */
export const AllGood: Story = {};
/** Each word once, a Frame in setup, and the problems worst first, each with its one fix and Details. */
export const Problems: Story = { args: { state: { kind: "ready", home: PROBLEMS, staleSince: null } } };
/** Central can't hear any Pi: one house problem replaces the Frames' own. */
export const HouseProblem: Story = { args: { state: { kind: "ready", home: HOUSE_PROBLEM, staleSince: null } } };
/** The first read is on its way: skeletons in the page's shape. */
export const Loading: Story = { args: { state: { kind: "loading" } } };
/** No Frame yet: the first step. */
export const Empty: Story = { args: { state: { kind: "ready", home: EMPTY, staleSince: null } } };
/** Central did not answer and nothing was ever read: one house card, no guess. */
export const NoAnswer: Story = { args: { state: { kind: "error" } } };
/** Central stopped answering: the last read, greyed, with since when (Can't tell). */
export const CantTell: Story = { args: { state: { kind: "ready", home: ALL_GOOD, staleSince: NOW - 360 } } };
/** All off in force on the hall: its banner with Resume schedule, its Frames resting. */
export const Held: Story = { args: { state: { kind: "ready", home: HELD, staleSince: null }, said: "Holding every display off." } };
/** A failure during Night off: Needs a look, and when it won't show (finding F1). */
export const WontShow: Story = { args: { state: { kind: "ready", home: WONT_SHOW, staleSince: null } } };
/** Restart app asked of a Pi: its tiles say requested, its steps come in with their times. */
export const RestartInProgress: Story = {
  args: {
    state: { kind: "ready", home: PROBLEMS, staleSince: null },
    requested: { "device-b": "Restart requested" },
    progress: (
      <StepProgress label="Restarting the photo app on Pi 77c0d1" steps={[
        { label: "Requested", state: "done", at: "21:10:02" },
        { label: "App stopped", state: "done", at: "21:10:04" },
        { label: "App started", state: "current" },
        { label: "Showing", state: "waiting" },
      ]} />
    ),
  },
};
/** Slice 3: a wall whose Frames can move, with Skip and Previous at its head and each tile's
 * everyday actions behind More. */
export const Everyday: Story = { args: { state: { kind: "ready", home: EVERYDAY, staleSince: null } } };
/** A wall's Skip just pressed: two tiles say requested until their Pi shows the new photo; one is
 * late, and a card says so with Retry. */
export const SkipRequested: Story = { args: { state: { kind: "ready", home: SKIP_REQUESTED, staleSince: null } } };
/** Show now on two Frames until 22:00 with **Back to normal**, and a pause with **Carry on**: the
 * paused Frame reads Showing, "Paused until 22:04". */
export const TakeoverAndPause: Story = { args: { state: { kind: "ready", home: TAKEOVER_AND_PAUSE, staleSince: null } } };
/** At phone width, slice 3: the wall's buttons wrap, each tile's menu is a sheet. */
export const EverydayPhone: Story = {
  args: { state: { kind: "ready", home: EVERYDAY, staleSince: null }, menuAs: "sheet", showNowIn: "tab" },
  parameters: { phone: true },
};
/** At phone width: one column, tiles two across, each menu a sheet; Show now is in the bottom tabs. */
export const Phone: Story = {
  args: { state: { kind: "ready", home: PROBLEMS, staleSince: null }, menuAs: "sheet", showNowIn: "tab" },
  parameters: { phone: true },
};
