import type { Meta, StoryObj } from "@storybook/react-vite";

import { SourceProblem } from "./source-problem";

const meta = {
  title: "Domain/SourceProblem",
  component: SourceProblem,
  args: {
    problem: {
      title:
        "Photo Wall refused this Source as too large (its current limit is at most 1,000 " +
        "matches), so it selects nothing",
      lines: [
        "It has no tags and no dates, so it asks for your whole Immich library.",
        "Immich refused the tag list: Photo Wall's Immich key is missing the tag.read " +
          "permission, so tags can't be picked until it is added (the setup guide's library " +
          "key step).",
        "Until then, narrow it with dates: edit it in Sources.",
        "Never refreshed successfully.",
      ],
    },
  },
} satisfies Meta<typeof SourceProblem>;

export default meta;
type Story = StoryObj<typeof meta>;

export const TooLarge: Story = {};
