import type { Meta, StoryObj } from "@storybook/react-vite";

import { Section } from "../ui/section";
import { DEFAULT_SETTINGS, type FramePowerView, PowerPanel } from "./frame-power";

const TEST = { request_id: "5b7f0c1e-2d4a-4f6b-9c8e-1a2b3c4d5e6f", power: "off" as const, for_seconds: 300,
  ends_at: 1_791_000_300 };

const IDLE: FramePowerView = {
  frame_id: "lobby",
  status: "idle",
  player_id: "player-a",
  output_id: "HDMI-A-1",
  display_id: "8b9e4f2d-1c3a-4b5e-8f7d-2a6c9e0b1d34",
  ...DEFAULT_SETTINGS,
  answers: ["ddc-ci", "signal-off"],
  method_in_use: "ddc-ci",
  test: null,
  result: null,
  in_force: { request_id: "standing", reason: "standing", power: "on", remaining_seconds: null },
  linked: true,
  last_heard_at: 1_791_000_000,
};

const OFF_IN_FORCE = { request_id: TEST.request_id, reason: "console-test" as const, power: "off" as const,
  remaining_seconds: 287 };

const meta = {
  title: "Domain/FramePower",
  component: PowerPanel,
  args: {
    view: IDLE,
    settings: DEFAULT_SETTINGS,
    saving: null,
    sent: null,
    sending: false,
    hardwareHref: "#/wall/frames/lobby/hardware",
    onTest: () => {},
    onSettings: () => {},
  },
  render: (args) => (
    <Section title="Power">
      <PowerPanel view={args.view} settings={args.settings} saving={args.saving} sent={args.sent}
        sending={args.sending} hardwareHref={args.hardwareHref} onTest={args.onTest} onSettings={args.onSettings} />
    </Section>
  ),
} satisfies Meta<typeof PowerPanel>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Best detected, the Pi using DDC/CI, displays on: no test. */
export const Idle: Story = {};
/** A test sent, the Pi not answering its change yet. */
export const Waiting: Story = { args: { view: { ...IDLE, status: "waiting", test: TEST } } };
/** The Pi's report answers the change: the display read back off. */
export const ConfirmedOff: Story = {
  args: { view: { ...IDLE, status: "answered", test: TEST, result: "confirmed", in_force: OFF_IN_FORCE } },
};
/** The method's channel gave no answer. */
export const DidNotAnswer: Story = {
  args: { view: { ...IDLE, status: "answered", test: TEST, result: "did-not-answer", in_force: OFF_IN_FORCE } },
};
/** No method answered the Pi's check: the smart plug comes later. */
export const NotSupported: Story = {
  args: { view: { ...IDLE, status: "answered", test: TEST, result: "not-supported", answers: [],
    method_in_use: null } },
};
/** A method chosen by hand, saved. */
export const MethodSaved: Story = {
  args: { settings: { ...DEFAULT_SETTINGS, power_method: "signal-off" },
    saving: { label: "Power method", state: "saved" } },
};
/** Central does not hold the Pi's link now: when it last heard from it. */
export const NotLinked: Story = {
  args: { view: { ...IDLE, status: "waiting", test: TEST, linked: false } },
};
/** Bound, but the Pi has not reported a display on the port yet. */
export const NoDisplayYet: Story = {
  args: { view: { ...IDLE, display_id: null, answers: [], method_in_use: null, in_force: null, last_heard_at: null } },
};
/** No Pi and HDMI port feeds this Frame. */
export const Unbound: Story = {
  args: { view: { ...IDLE, status: "unbound", player_id: null, output_id: null, display_id: null, in_force: null } },
};
