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
    // S2: only primitives and patterns style. A domain component or a page composes them and
    // writes no className or style, spreads no props onto an element (a spread could carry
    // either), builds no element by hand and imports no class helper from the catalog.
    files: files(["domain", "pages"]),
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: "JSXAttribute[name.name=/^(className|style)$/]",
          message: "Pages and domain components do not style: compose primitives and patterns.",
        },
        {
          selector: "JSXSpreadAttribute",
          message: "Pages and domain components spread no props: a spread can carry a className or style.",
        },
        {
          selector: "CallExpression[callee.name=/^(createElement|cloneElement)$/], "
            + "CallExpression[callee.property.name=/^(createElement|cloneElement)$/]",
          message: "Pages and domain components build elements in JSX, where this lint reads them.",
        },
      ],
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            {
              group: ["**/ui/*", "**/patterns/*"],
              importNamePattern: "^(cn|\\w+Variants|severity[A-Z]\\w*)$",
              message: "Class helpers belong to primitives and patterns: compose their components.",
            },
          ],
        },
      ],
    },
  },
  {
    // Primitives and patterns style with classes from the tokens, never an inline style.
    files: files(["ui", "patterns"]),
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: "JSXAttribute[name.name='style']",
          message: "Style with token classes (src/design/tokens.css), not an inline style.",
        },
      ],
    },
  },
);
