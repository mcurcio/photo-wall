import type { Meta, StoryObj } from "@storybook/react-vite";

import { Button } from "../ui/button";
import { Field, TextInput } from "../ui/field";
import { EditorPage } from "./editor-page";
import { EntityHeader } from "./entity-header";
import { MediaGrid } from "./media-grid";
import { SaveBar } from "./save-bar";

function photo(width: number, height: number, shade: number): string {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}">`
    + `<rect width="${width}" height="${height}" fill="rgb(${shade},${shade},${shade})"/></svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

const ITEMS = [[3, 2], [4, 3], [9, 16], [1, 1], [16, 9]].map(([w, h], index) => ({
  key: `p${index}`,
  src: photo(w * 30, h * 30, 70 + index * 20),
  alt: `Photo dated ${index + 1} Mar 2025`,
  width: w * 1000,
  height: h * 1000,
}));

const meta = {
  title: "Patterns/EditorPage",
  component: EditorPage,
  args: {
    header: <EntityHeader title="New Photo source" />,
    form: (
      <Field label="Name" help="What Scenes and Show now call this Source.">
        <TextInput value="garden" width="long" onChange={() => {}} />
      </Field>
    ),
    previewTitle: "Photos in this Source",
    previewLine: "2,301 photos · 14 videos",
    preview: () => <MediaGrid label="Photos in the pool" items={ITEMS} />,
    saveBar: (
      <SaveBar label="Source actions">
        <Button variant="primary">Save Photo source</Button>
        <Button variant="ghost">Discard changes</Button>
      </SaveBar>
    ),
  },
} satisfies Meta<typeof EditorPage>;

export default meta;
type Story = StoryObj<typeof meta>;

/** At the catalog's 800 px (between `md` and `lg`) the preview sits under the form. */
export const Default: Story = {};
