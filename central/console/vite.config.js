import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";

// The built bundle is served same-origin at /console by central/app.py under a
// strict CSP: `default-src 'self'; script-src 'self'; style-src 'self'
// 'unsafe-inline'`. Three build choices keep the bundle CSP-legal:
//   - base "/console/": hashed asset URLs resolve under the /console/assets/…
//     path that app.py serves.
//   - modulePreload.polyfill false: Vite's default module-preload polyfill is an
//     INLINE <script>, which `script-src 'self'` would block (and then the app
//     never mounts). Disabling the polyfill leaves the entry as a single external
//     `<script type="module" src="/console/assets/index-<hash>.js">`.
//   - assetsInlineLimit 0: Vite otherwise inlines small assets (the font, icons)
//     as `data:` URLs, which `default-src 'self'` refuses; every asset is a file.
// Tailwind compiles at build time to a static stylesheet (no runtime style injection). The
// "@/" alias is the catalog layers' (components.json, tsconfig.json `paths`).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  base: "/console/",
  build: {
    modulePreload: { polyfill: false },
    assetsInlineLimit: 0,
  },
});
