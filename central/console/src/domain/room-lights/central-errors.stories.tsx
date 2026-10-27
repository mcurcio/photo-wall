import type { Meta, StoryObj } from "@storybook/react-vite";

import { CLEAR, FAMILY_ROOM, IMPEDED, IN_DOUBT, LIGHT_NAMES, NOT_REPORTED } from "./central-errors.fixtures";
import { FrameCentralError } from "./frame-central-error";
import { causeAndFix, subjectWords } from "./model";
import { SourcesInDoubt } from "./sources-in-doubt";

/* Central's errors: Needs attention's source rows and the Frame page's "Central's error". */

const meta = {
  title: "Domain/CentralErrors",
  component: SourcesInDoubt,
  args: {
    read: { view: IN_DOUBT, failed: false },
    names: LIGHT_NAMES,
    since: () => "Tue 18:05",
    frameLink: (frameId: string) => <a href={`#/wall/frames/${frameId}/overview`}>{`Frame ${frameId}`}</a>,
    lightsHref: "#/lights",
  },
} satisfies Meta<typeof SourcesInDoubt>;

export default meta;
type Story = StoryObj<typeof meta>;
// Taller than the catalogue's 800 x 600 view: screenshot whole (tests/browser/test_console_catalog_browser.py).
const WHOLE = ["full-page"];

/** Can't read (Central's error, with the side to update), can't tell, and a link closed. */
export const NeedsAttention: Story = { tags: WHOLE };
/** Tonight's case alone: Photo Wall can't read Family Room Lights; update Galleria. */
export const CantRead: Story = { args: { read: { view: { in_doubt: [FAMILY_ROOM] }, failed: false } } };
/** Central did not answer, never read: never "nothing needs attention". */
export const ReadFailed: Story = { args: { read: { view: null, failed: true } } };
/** The last read failed: the rows last read, under a line saying so. */
export const ReadFailedKept: Story = { args: { read: { view: { in_doubt: [FAMILY_ROOM] }, failed: true } } };
/** At phone width. */
export const Phone: Story = { tags: WHOLE, parameters: { phone: true } };

/** The Frame page's Central's error: its Pi reports the Frame black under Central's card. */
export const FrameImpeded: Story = {
  render: () => <FrameCentralError read={{ view: IMPEDED, failed: false }} names={LIGHT_NAMES} attentionHref="#/attention" lightsHref="#/lights" />,
};
/** Its Pi reports no error of Central's blacks the Frame. */
export const FrameClear: Story = { render: () => <FrameCentralError read={{ view: CLEAR, failed: false }} names={LIGHT_NAMES} attentionHref="#/attention" lightsHref="#/lights" /> };
/** Its Pi has not said (an older release). */
export const FrameNotReported: Story = {
  render: () => <FrameCentralError read={{ view: NOT_REPORTED, failed: false }} names={LIGHT_NAMES} attentionHref="#/attention" lightsHref="#/lights" />,
};
/** Central did not answer the Frame's read. */
export const FrameReadFailed: Story = { render: () => <FrameCentralError read={{ view: null, failed: true }} names={LIGHT_NAMES} attentionHref="#/attention" lightsHref="#/lights" /> };

/** The rows' words are Central's facts and advice, never judged here: a pure check the catalogue runs
 * before any story renders. */
const checks: [string, string][] = [
  [subjectWords(FAMILY_ROOM, LIGHT_NAMES), "Family Room Lights"],
  [subjectWords({ ...FAMILY_ROOM, lights: [] }, LIGHT_NAMES), "light.family_room"],
  [causeAndFix(FAMILY_ROOM),
    "Photo Wall can't read what its source reports. Update Galleria in Home Assistant (HACS) to its newest release."],
];
for (const [said, meant] of checks) {
  if (said !== meant) throw new Error(`central error words: ${JSON.stringify(said)} is not ${JSON.stringify(meant)}`);
}
