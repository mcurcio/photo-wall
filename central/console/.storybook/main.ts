import type { StorybookConfig } from "@storybook/react-vite";

// The catalog (console design system): every primitive, pattern and domain component in
// each of its states. It builds with the console's own Vite config (Tailwind, the "@/"
// alias); only the base differs, since the static build is browsed from any path.
const config: StorybookConfig = {
  stories: ["../src/**/*.stories.@(ts|tsx)"],
  framework: { name: "@storybook/react-vite", options: {} },
  core: { disableTelemetry: true },
  viteFinal: async (viteConfig) => ({ ...viteConfig, base: "./" }),
};

export default config;
