// The console's lint (console design system S2, S3), over the catalog layers only:
// src/design, src/ui, src/patterns, src/domain, src/pages. The legacy modules at the top of
// src/ are not linted yet; the design system's end condition removes that exemption.
import js from "@eslint/js";
import betterTailwindcss from "eslint-plugin-better-tailwindcss";
import tseslint from "typescript-eslint";

const LAYERS = ["design", "ui", "patterns", "domain", "pages"];
const files = (layers) => layers.map((layer) => `src/${layer}/**/*.{js,jsx,ts,tsx}`);

export default tseslint.config(
  { ignores: ["dist/", "storybook-static/", "node_modules/"] },
  {
    files: files(LAYERS),
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: { parserOptions: { ecmaFeatures: { jsx: true } } },
    plugins: { "better-tailwindcss": betterTailwindcss },
    settings: { "better-tailwindcss": { entryPoint: "src/styles.css" } },
    rules: {
      // S3: colour means something, or it does not exist. The theme clears Tailwind's
      // palette, so `bg-red-500` is unknown; an arbitrary value (`bg-[#f00]`,
      // `text-[red]`, `[color:red]`) bypasses the tokens and is refused.
      "better-tailwindcss/no-unknown-classes": "error",
      "better-tailwindcss/no-restricted-classes": [
        "error",
        {
          restrict: [
            {
              pattern: "^.*\\[.*$",
              message: "Arbitrary value in '$0': use a design token (src/design/tokens.css).",
            },
          ],
        },
      ],
    },
  },
  {
    // S2: only primitives and patterns style. A domain component or a page composes them
    // and writes no className or style.
    files: files(["domain", "pages"]),
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: "JSXAttribute[name.name=/^(className|style)$/]",
          message: "Pages and domain components do not style: compose primitives and patterns.",
        },
      ],
    },
  },
);
