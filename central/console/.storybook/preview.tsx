import type { Decorator, Preview } from "@storybook/react-vite";

// The app's one stylesheet entry, so a member looks the same here and in the app.
import "../src/styles.css";

/**
 * The colour scheme: the toolbar sets `data-scheme` on the root, the attribute
 * design/tokens.css reads (the app leaves it unset and follows the system).
 */
const withScheme: Decorator = (Story, context) => {
  document.documentElement.dataset.scheme = context.globals.scheme as string;
  return <Story />;
};

/**
 * A phone's width (384 px, under the console's 768 px phone switch): a story with
 * `parameters: { phone: true }` draws inside it, so a page or domain story shows its phone
 * layout without styling of its own (S2). A container-query layout (WeekCalendar) follows it.
 */
const withPhone: Decorator = (Story, context) =>
  context.parameters.phone === true ? <div className="w-96"><Story /></div> : <Story />;

const preview: Preview = {
  globalTypes: {
    scheme: {
      description: "Colour scheme",
      toolbar: {
        title: "Scheme",
        icon: "mirror",
        items: [
          { value: "dark", title: "Dark" },
          { value: "light", title: "Light" },
        ],
        dynamicTitle: true,
      },
    },
  },
  initialGlobals: { scheme: "dark" },
  decorators: [withScheme, withPhone],
  parameters: { layout: "padded" },
};

export default preview;
