import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built bundle is served same-origin at /console by central/app.py under a
// strict CSP: `default-src 'self'; script-src 'self'; style-src 'self'
// 'unsafe-inline'`. Two build choices keep the bundle CSP-legal:
//   - base "/console/": hashed asset URLs resolve under the /console/assets/…
//     path that app.py serves.
//   - modulePreload.polyfill false: Vite's default module-preload polyfill is an
//     INLINE <script>, which `script-src 'self'` would block (and then the app
//     never mounts). Disabling the polyfill leaves the entry as a single external
//     `<script type="module" src="/console/assets/index-<hash>.js">`.
export default defineConfig({
  plugins: [react()],
  base: "/console/",
  build: {
    modulePreload: { polyfill: false },
  },
});
