# 0018 — The console by domain, and its design system

**Date:** 2026-10-09 · **Layer:** system (the console's information architecture and how its pages are built) · **Status:** Owner-approved on the two gate pages below, 2026-10-09. Milestone 1 (the design-system foundation, DS0) is built; later milestones follow the migration below. The layouts are a starting point (see [Layouts are a starting point](#layouts-are-a-starting-point)).

**Gate pages:** [Console by Domain](https://claude.ai/artifact/Dx6gHA8T31WDmAGccyg1TY) · [Console design system](https://claude.ai/artifact/B9GbkgD3d66wsh5iiwzwjh). The page text is kept in the repository as the design inputs `.claude/runs/console-by-domain-design.md` and `.claude/runs/console-design-system.md`; the owner's words are in `.claude/runs/console-owner-notes.md`.

## The problem

The console showed one Pi as one vertical page: every layer of the Pi's stack on one screen, with a Frame's setup and its live state on the Wall, per-Frame health repeated on Now, and a single Frame health label mixing "needs calibration" with "Player app silent". Under it, a 2,620-line stylesheet named its classes for pages (`player__`, `roster__`, `facet__`), unlayered element rules styled every button, and severity was typed four times in two vocabularies.

## The owner's words

- 2026-10-08: "I still don't like the way that the node stack is represented in the Central UI. it's too "vertical" where every part of the pi stack is all on one screen, where I want the UI to present a more "horizontal" structure where the UI is organized by the conceptual layers/domains and each includes all of the relevant hardware."
- 2026-10-09: "I think software and apps go together. Hardware and connection go together. Screens is related to show runs. And there probably needs to be other screens for show creation."
- 2026-10-09: picking one Pi is a focus filter: "There are inevitably some per-pi specific detail pages as you drill down". New Pis go inside Connection.
- 2026-10-09: "Consider the feature domains. Frames are a calibration concern in one flow AND a runtime surface in another perspective"
- 2026-10-09: "I answered the questions you posed, but I also want to note that this design is just a starting point. As new capabilities are added to the hardware and to Central, we will need to constantly re-evaluate how pieces are laid out to maximize the user experience. To that end, I would encourage focusing on a design language and building a catalog of react primitives rather than hard coding specific pages. It might be good to find a strong design library (tailwind? Bootstrap?) and build out the design system."
- 2026-10-09: "I want the look-and-feel to feel like it's related to immich"

## The domains

The console is cut by feature domain, not by box. Each domain is a bounded context; a Frame or Pi that several domains show appears in each with only that domain's own facts (a projection, not a second home). The sidebar is this table.

| Domain | The one question | Facts it owns | Actions it owns |
|---|---|---|---|
| **Wall setup** | Is every Frame placed, bound to an Output and calibrated? | placement, Binding, calibration, Frame profile, setup state, free Outputs | Edit layout, Bind, Unbind, Identify Panel, Calibrate |
| **Screens** | What is each Frame presenting now, and why? | live health, the Run on top, Why, what Display Host reports | none |
| **Now** | Which Runs are active, and is media flowing? | Runs and Activations, media pipeline | Show now, finish and cancel a Run |
| **Show creating** | What will the show be? | Scenes, Programs, Sources | create, edit, delete |
| **Hardware** | Is every Pi powered, healthy and linked? | host samples and facts, Node API link, Host Management sessions, standing, reboot history | Reboot, Retire |
| **Software** | Is every Pi running the selected release, and did the last change take? | boot offer and steps, base, kernel, Player app, app operations, qualification, boot selection, effect gate | Stage app, Qualified fallback, Select, Update the wall |
| **Needs attention** | What lost something it had? | none (links only) | none |

**Sidebar:** Show running (Screens, Now) · Show creating (Scenes, Schedule, Sources) · Wall · Fleet (Hardware, Software) · Needs attention.

The rules that keep the cut honest: **H1**, a fact is judged, explained and acted on only in its owning domain, and elsewhere is only a read-only link naming it; **H2**, a Pi focus lives only in the address; **H3**, Screens and Now only read what a Panel presents, and every write that changes a Panel is Wall setup. Frame health splits in two: live health (Screens) and setup state (Wall).

## The answers

| Question | Answer |
|---|---|
| Which page opens first | **Screens**, once a Frame exists; with no Frame, the Wall's first step (Add first frame) |
| Pages for creating a show | **Deferred** to their own design pass; Scenes, Schedule and Sources stay as they are |
| Binding from the Pi side | **Bind and unbind only from the Frame on the Wall**; Hardware links there |
| UI kit | **Tailwind + shadcn/ui on Base UI**, with TanStack Table for dense rows |
| Look | **Keep today's look** (dark first, Console Sans, rounded cards), re-expressed from the photo library's theme; the owner wants it to feel related to Immich. Tokens carry it over, and a new look is later mostly a token change |

## The design system

A design language (tokens) and a catalog of primitives, patterns and domain components; pages are compositions of the catalog. Five layers; imports point down only (prior art: Feature-Sliced Design), under `central/console/src/`:

| Layer | Directory | Holds | Knows |
|---|---|---|---|
| Pages | `pages/` | compositions per route | domain components and patterns |
| Domain components | `domain/` | one domain's model rendered through patterns (FactLine, PiHeader, WallPlan) | one domain's model |
| Patterns | `patterns/` | recurring layouts (EntityList, EntityPage, FocusFilter, LinkToOwner, HealthBadge) | plain data and links, no console model |
| Primitives | `ui/` | Button, Table, StatusChip, Dialog (shadcn output on Base UI) | no Photo Wall concept |
| Tokens | `design/` | `tokens.css` and its TypeScript module: the only place a raw value lives | nothing |

One severity scale (`ok`, `todo`, `notice`, `alarm`, `unknown`) replaces the four typedefs; a domain component hands a pattern a verdict whose words are its model's, so a pattern never re-judges (H1).

| Rule | Enforced by | Strength |
|---|---|---|
| **S1.** Imports point down; primitives and patterns import no console model (`facts`, `health`, `hostHealth`, `join`, `players`, `routes`); a domain component imports one domain's | the import-graph test `tests/test_console_routes_r4.py` | test |
| **S2.** Only primitives and patterns style: pages and domain components write no `className` or `style` | ESLint `no-restricted-syntax` on JSX attributes (`central/console/eslint.config.js`) | lint |
| **S3.** Colour means something, or it does not exist: only semantic tokens | the theme clears Tailwind's palette, plus `eslint-plugin-better-tailwindcss` `no-unknown-classes` and a restriction on arbitrary values | lint |

S3's palette clearing alone is not a check: an unknown class is dropped without an error, so the lint is what fails. S2 and S3 lint the five layer directories only; the legacy modules at the top of `src/` are exempt until the end condition below.

The catalog is browsed in Storybook and tested by `tests/browser/test_console_catalog_browser.py` walking its static build (render, axe-core, screenshot diff), in its own CI leg `console-catalog`. The leg's time is measured by the tracer and sharded by layer to stay under about 4 minutes. The console's static checks (`npm run typecheck`, `npm run lint`) run on Node 22.

## Migration

By layer for the look, by page group for layout:

| Stage | What changes | True after it |
|---|---|---|
| DS0 | the token file is the only token source; `index.css` sits in a cascade layer beneath Tailwind's utilities; the catalog loads the app's one stylesheet entry | a member looks the same in the catalog and in the app |
| DS2 | primitives replace the global element rules (button, input, link, table, details, dialog) in one sweep | no element is styled two ways |
| DS3+ | each page group moves whole to domain components and patterns; its old CSS is deleted in the same bead | no page in two styles, no flag, no alias |

The fleet work (Hardware, then Software) and the Frame live face (Screens, Wall setup-only, Now Run-keyed) are the new pages and are built from the catalog; the old Players pages and routes are deleted with them, with no aliases.

**End condition:** `index.css` and its layer are gone, Tailwind's reset is on, and the S2 and S3 lints run with no exemption.

## Layouts are a starting point

The owner's words bind here: this layout is "just a starting point", to be re-evaluated as capabilities are added to the hardware and to Central. That is why the catalog and tokens are the product and the pages are compositions: moving a section is an edit to a page, not to the look.

## Costs

- A Frame has two pages (setup, live) and a Pi has two (Hardware, Software); under H1 each shows the other only as a link, so "why is Frame X dark" can take Screens, Hardware, then Software.
- The landing, the Wall's daily face and Now's Frame regions move; the plan is drawn twice; every fleet route and its tests move.
- Three backend read changes (link state, and app with latest operation, on the fleet host read; a fleet presentation read for Display Host's report).
- React moves to 19 first; we own roughly 20 to 30 copied component files and merge upstream fixes by hand; ESLint and `tsc` join the static tier; Node 22 builds the console.
- Old and new styling, and JavaScript and TypeScript, coexist until the end condition; screenshot baselines regenerate when the reset lands.
- We write our own story walker and pixel diff rather than use Vitest's built-ins.
- Given up: Base UI's accessibility is less tested than React Aria's (shadcn keeps both bases, so a component can switch later). Deferred: whole-page screenshots, a density toggle.

## What it supersedes

- [Operator console DDD](../operator-console-ddd.md): pass 1's "Q1 = A, one home per box", the per-box Player page (§9, the §48 tree, §50, §52) and the Wall-first landing. The DDD gains [Part I](../operator-console-ddd.md#part-i-the-console-by-domain-0018) holding the domain table; sections the later milestones change in detail are not rewritten until they land.
- [Operator console UX design](../operator-console-ux-design.md): one Player page per box, and §8a Wall-first (becomes setup-first, with Screens opening the console).
