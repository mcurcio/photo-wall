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
  decorators: [withScheme],
  parameters: { layout: "padded" },
};

export default preview;
