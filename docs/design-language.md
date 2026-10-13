# Console design language

**Status:** proposed, 2026-10-09; nothing new here is built. This is the one home for the console's UI rules: principles, page templates, the pattern catalogue, the save models and which concept uses which, status words and their order, and the words the console uses. It builds on [decision 0018](decisions/0018-console-by-domain-and-design-system.md) (layers, tokens, severity scale, rules S1 to S3, the look) and never contradicts it. **What** each page holds is the [operator console design](operator-console-design.md); **when** it is built is the [roadmap](roadmap.md). The procedure that applies this document to one feature is the project skill `.claude/skills/console-ux/SKILL.md`.

The owner, 2026-10-09 (chat): "This roadmap needs to be matched with a clear design language package, and a design skill that knows how to translate these individual feature concepts into a normalized UX". Earlier the same day: "i continue to be frustrated with how poorly designed the UI is." and "I would encourage focusing on a design language and building a catalog of react primitives rather than hard coding specific pages" ([0018](decisions/0018-console-by-domain-and-design-system.md#the-owners-words)).

Everything below except those quotes is a design choice.

**Contents:** [1 Principles](#1-principles) · [2 Foundations](#2-foundations) · [3 Page templates](#3-page-templates) · [4 Component and pattern catalogue](#4-component-and-pattern-catalogue) · [5 Interaction rules](#5-interaction-rules) · [6 Status and severity](#6-status-and-severity) · [7 Content and voice](#7-content-and-voice) · [8 Concept → UX translation](#8-concept--ux-translation) · [9 Enforcement](#9-enforcement)

## 1. Principles

Each principle has a test; a design or a page that fails the test is wrong, whatever it looks like. How strong each test is (compile, lint, test or review) is [§9](#9-enforcement).

| # | Principle | Test | Prior art |
|---|---|---|---|
| P1 | **One home per setting.** Every setting and every fact is changed in exactly one place; anywhere else it appears as a read-only line that links to its home. | The [settings catalogue](operator-console-design.md#11-settings-catalogue) names one "Lives on" per setting; no page renders a control for a setting whose home is elsewhere (it renders `LinkToOwner`). | 0018's H1; Home Assistant device pages link to an entity's one settings dialog |
| P2 | **Show where a setting acts.** A control that changes equipment says what it belongs to (this Frame, this display) and where it takes effect (on the display, on the Pi, or in the picture Photo Wall sends). Where a page holds both groups of picture settings, the section says it: **Display (hardware)** and **Photo Wall (software)** ([0022](decisions/0022-picture-hardware-and-software-apart.md)). | `SettingRow`'s equipment variant requires `belongsTo` and `actsOn` ([§4](#settingrow)); the Picture tab's two sections each hold one group. | macOS Displays and TV menus name the device; Polaris "explain the consequence" |
| P3 | **Live on the device, then Done or Revert.** A change to a display shows on the real display while you make it; **Done** keeps it and is offered only once the Pi acknowledges it presents the latest change. | `LivePreviewEditor` derives Done from the acknowledgement of the latest revision ([§4](#liveprevieweditor)). | Apple HIG Display settings; [U9](requirements.md#failure-visibility-and-recovery) |
| P4 | **Say what is true; never claim pixels.** Every status names its source and time; what nobody reported is **Can't tell since …**, never a guess. The console says what the Pi reports it presents, never what the display lights up. A request is never shown as done before its device acknowledges it. | Every status carries a time; "on screen" is a [forbidden word](#forbidden-words); device actions use `AckBadge`. | [U6](requirements.md#failure-visibility-and-recovery); Home Assistant's "Unavailable" state |
| P5 | **Plain words.** Labels come from the glossary; internal words appear only under **Details**. | The [forbidden words](#forbidden-words) check. | GOV.UK content design: "use plain English" |
| P6 | **One status vocabulary, one colour meaning.** Six status words drawn on 0018's severity scale; colour means status and nothing else; the accent colour means "you can act here". | The status words are a typed domain value ([§6](#6-status-and-severity)); S3; accent classes only in an allow list of primitives. | Atlassian lozenges (one colour per meaning); Polaris tone |
| P7 | **Every page is a template.** A page's root is one template component from [§3](#3-page-templates), filled with catalogue patterns; a feature that fits no template is a design question raised before code, not a new layout. | A page's default export renders one `patterns/templates/*` component; each template takes a `LoadState` and has a story per state. | GOV.UK patterns ("use a pattern before designing your own"); Polaris page templates |

## 2. Foundations

The values live in [`tokens.css`](../central/console/src/design/tokens.css) and its TypeScript vocabulary [`tokens.ts`](../central/console/src/design/tokens.ts) and nowhere else (0018, S3). The design layer knows no Photo Wall concept. This section names the roles; it never copies a value.

| Foundation | Today (tokens.css) | Rule | Gap to close |
|---|---|---|---|
| **Colour roles** | Surfaces (`surface`, `-raised`, `-sunken`, `-input`); text (`text`, `label`, `muted`); lines (`line`, `line-input`); `accent`, `accent-tint`, `on-accent`, `focus`; severity (`ok`, `todo`, `notice`, `alarm`, `unknown`, `on-alarm`); truth kinds; `shadow` | Page = `surface`; a card or tile = `surface-raised`; a well (preview, log) = `surface-sunken`. Severity colours only through `StatusChip`, `HealthBadge` and the helpers in `ui/severity.tsx`; `todo` is orchid, its own hue, so unfinished setup never reads as the accent's blue (owner, 2026-10-11: "Recolour it"). Accent only for actions, links, selection and focus, and only inside the primitives that draw them. | A `Link` primitive, so patterns stop writing `text-accent` ([§9](#9-enforcement)) |
| **Type scale** | Console Sans (Immich's Google Sans); sizes are Tailwind's defaults, used 25 times across `ui` and `patterns` | Five roles: page title, section title, body, label, meta (receipts, units). Pages never choose a size (S2 already forbids `className` there). | Role tokens; the cost and the alternative are in [§9](#9-enforcement) |
| **Spacing and density** | `--spacing: 4px` (Tailwind step); `--spacing-row: 8px` | One density. Touch targets at least 44 px on phone (WCAG 2.5.5, Apple HIG); table rows keep `spacing-row` on desktop. A density toggle stays deferred (0018). | none |
| **Radius** | `button`, `input`, `card`, `pill` | Chips are `pill`; tiles and cards are `card`. | none |
| **Motion** | none | 150 ms ease-out for state changes (sheet in, toast in, tab change); no motion carries meaning alone; `prefers-reduced-motion` turns it off. Live previews never animate (the display is the preview). | `--duration-fast` and `--ease-standard` tokens |
| **Icons** | none | Deferred to delivery 3b-i (the first one that needs icons: tile actions on Home). Recommendation recorded: Material Design Icons (`@mdi/js`), the set Immich's web app uses, behind one `ui/icon` primitive with a required label or `decorative`. Cost: shadcn's copied components import lucide, so each swaps its few icons by hand. Until then actions are words. | Decide at 3b |
| **Dark and light** | Dark first; light overrides the same tokens; follows the system unless `data-scheme` is set | Every member works in both; the catalogue walker screenshots and runs axe in both schemes for every story. | none |
| **Breakpoints** | Tailwind's defaults (not cleared) | One switch: below `md` (768 px) is **phone**: bottom tabs, tables become cards, sheets come from the bottom, editors stack preview above controls. No other breakpoint changes structure. | Name the switch as a token |

## 3. Page templates

The normalized page shapes. Each is a catalogue component in `central/console/src/patterns/templates/`, so a page's shape and its states are checkable (P7). Navigation (the rail, the phone tabs, which section a page sits in) is the [operator console design §4](operator-console-design.md#4-navigation). Two recurring shapes that live **inside** a page are patterns, not templates: the [live device editor](#liveprevieweditor) (a tab of an Object page) and the [quick-action sheet](#quickactionsheet) (opened from anywhere).

**The template contract.** Every template takes its data as one `LoadState` and renders every branch, so no page can skip a state:

```ts
type LoadState<T> =
  | { kind: "loading" }                                     // skeletons in the layout's shape, never a spinner over a blank page
  | { kind: "empty"; empty: EmptyStateProps }                // the one next step
  | { kind: "error"; problem: ProblemCardProps }             // Central could not answer, with Retry
  | { kind: "ready"; data: T }
  | { kind: "stale"; data: T; since: string };               // Can't tell: last values kept, greyed, "Can't tell since 21:04"
```

Each template's stories include `Loading`, `Empty`, `Error`, `Ready`, `CantTell` and `Phone`.

### Choosing a template

| The concept is mainly… | Template |
|---|---|
| "Is it OK and what is on the wall right now?" | [T1 Home](#t1-home) |
| Many things of one kind, to find one or act on several | [T2 Object list](#t2-object-list) |
| One thing the user manages (a Frame, a Pi), including adjusting its display | [T3 Object page](#t3-object-page) (with `LivePreviewEditor` in a tab) |
| Authoring content with many fields and a preview (Scene, Photo source) | [T4 Editor](#t4-editor) |
| Something over time: blocks, lanes, holds, "why now" | [T5 Timeline](#t5-timeline) |
| Where things hang: arranging Frames to scale on a Wall | [T6 Layout canvas](#t6-layout-canvas) |
| First-run steps that each turn green from a real check | [T7 Setup checklist](#t7-setup-checklist) |
| House-wide configuration of one area (Photo library, Integrations, Backups) | [T8 Settings area](#t8-settings-area) |
| A short, frequent action from anywhere | not a page: [`QuickActionSheet`](#quickactionsheet) |

**Listed exceptions** (no template, and why): **Sign-in** is a standalone form outside the app shell (nothing else can render before it; GOV.UK sign-in pattern). **Phone layout** (F6) is a property of `AppShell` and every template, not a page.

### T1 Home

- **Anatomy:** house word + one sentence; `ProblemCard`s pinned on top (worst first); `OverrideBanner`s for holds and takeovers; the `WallMap` per Wall; a now/next strip; quick actions (Show now, All off/on).
- **Use** for the console's landing only. **Not** for setup tasks (T7) or settings.
- **States:** Empty = no Frame yet → the Setup checklist's next step. Error = Central unreachable → one house banner replaces per-tile alerts. Can't tell = per tile.
- **Phone:** one column; tiles two across; quick actions in the bottom tabs ([design §4](operator-console-design.md#4-navigation)).
- **Prior art:** Home Assistant dashboard tiles; Atlassian Statuspage header.

### T2 Object list

- **Anatomy:** `EntityHeader` (title, add action); optional `FocusFilter`; `EntityList` grouped, worst first; bulk actions on selection; each row opens its T3 page.
- **Use** for Frames, Pis, New Pis waiting, Scenes, Photo sources, Groups, the Never show list (rows as a `MediaGrid`). **Not** when there is only ever one of the thing.
- **Phone:** each row becomes a card: name, status chip, one secondary fact; no sideways scrolling.
- **Prior art:** Polaris IndexTable (cards on small screens); GOV.UK summary list.

### T3 Object page

- **Anatomy:** `EntityHeader` (editable name, where it sits, status, live view where one exists, two or three header actions) then `Tabs`. The header's title is the page's one top heading: the template's shell takes `ownsHeading={false}` and renders no section heading of its own, and `EntityHeader` takes `level` so the title is an `h1` here and an `h2` where a header sits inside another page; **one tab = one concern** (the Frame page's tabs are [design §5](operator-console-design.md#5-the-frame-page)). The first tab is Overview (read-only). Each tab has its own `LoadState`. Irreversible actions sit in a danger zone (a `Section` with `tone="danger"`) at the bottom of the last tab.
- **Use** for anything the user manages as a thing. **Not** for authoring content (T4).
- **States:** besides the contract, an unbound Frame shows every tab with the binding picker where equipment is needed (design §5); a Pi that is offline is a feature state, not an Error: the tab stays, controls that need the Pi are disabled with "Can't reach Pi pw-3f2a since 21:04".
- **Phone:** tabs scroll as a strip; header collapses to name + status; header actions move to an overflow menu except the first.
- **Prior art:** Home Assistant device page (controls, sensors, configuration, diagnostics); signage "screen settings" pages.

### T4 Editor

- **Anatomy:** `EntityHeader` (name, kind); `Section`s of `SettingRow`s in a fixed order of concern; a live preview (`MediaGrid` with count, or a preview on a chosen Frame) beside it; a `SaveBar`.
- **Use** for Scenes and Photo sources. **Not** for equipment (T3) or single toggles.
- **States:** the preview has its own `LoadState`; its Empty is "No photos match", naming the filter that removed most.
- **Phone:** preview collapses to a count bar that opens the grid; sections become disclosures.
- **Prior art:** Polaris contextual save bar; Immich's search filters.

### T5 Timeline

- **Anatomy:** a time axis with a now line; `TimelineLane`s (the Power lane on top, then content lanes); a block opens its editor in a `Sheet`; `OverrideBanner` for holds; "Why is this playing?" opens an explanation sheet.
- **Use** for the Schedule page. A Frame's day on its Overview tab is a single `TimelineLane` inside T3, not a second Timeline page.
- **States:** Empty = only the Default Scene band, with "Add a Schedule"; Can't tell = lanes hatched after the last report.
- **Phone:** one day at a time, vertical axis, swipe between days.
- **Prior art:** Google Calendar week view; thermostat schedules with hold and resume (ecobee, Nest).

### T6 Layout canvas

- **Anatomy:** a Wall tab per wall plus "+ Wall"; a canvas drawn to real scale in house units; `WallTile`s dragged and resized, with keyboard nudge; overlaps flagged; a side panel for the selected Frame's placement fields; Identify on each Frame.
- **Use** for Frames › wall layout (C1). **Not** for Home's live map (T1 uses `WallMap` read-only).
- **States:** Empty = "Add your first Frame"; Can't tell does not apply (placement is Central's own data).
- **Phone:** view and select only; placement edits through the side panel's fields, not drag.
- **Prior art:** macOS Displays arrangement; Home Assistant floor plans.

### T7 Setup checklist

- **Anatomy:** a `Checklist` of steps: title, one-line why, status from a real check (never a click), the action that fixes it; the next step expanded.
- **Use** for first-run setup. **Not** for ongoing health (T1).
- **States:** each step Not started, Checking, Done (with when), Problem (`ProblemCard` inline, `todo` severity); the page hides itself when done (design §9).
- **Phone:** one step open at a time.
- **Prior art:** GOV.UK task list; Polaris setup guide.

### T8 Settings area

- **Anatomy:** title; `Section`s of `SettingRow`s; a connection area has a live check list ("Broker connected → Home Assistant online → 12 devices published") and a **Test** button with a counted result; a danger zone last.
- **Use** for every Settings › area. **Not** for settings of one Frame or one Scene (they live on that object).
- **States:** Empty = not set up (an `EmptyState` naming what it gives you); Testing; Connected (with counts); Problem (the error template).
- **Phone:** one column, rows full width.
- **Prior art:** Immich's administration settings; Home Assistant integration entries; Polaris annotated sections.

## 4. Component and pattern catalogue

Layers and import rules are 0018's. A **primitive** (`src/ui`) knows no Photo Wall concept; a **pattern** (`src/patterns`, templates in `src/patterns/templates`) takes plain data and links; a **domain component** (`src/domain`) turns one domain's model into pattern props. New primitives come from shadcn on Base UI where one exists. Every member has a story file; the catalogue walker renders each story in dark and light, runs axe and diffs a baseline.

Members load-bearing in deliveries 1a to 1c have a props sketch below; every other member is named with its purpose and the delivery that introduces it, and its props are written when that delivery starts.

### Existing members

| Member | Layer | Keep / change |
|---|---|---|
| `Button` | ui | Keep. Add `size: "default" \| "touch"` (44 px on phone). One `primary` per view; `danger` only in danger zones and `ConfirmDangerous`. |
| `Dialog` | ui | Keep (its in-flight rule stands). Base of the dialog guards. |
| `Disclosure` | ui | Keep: the one way to show **Details ›**. |
| `Section` | ui | Keep (an error boundary whose fallback stays a plain primitive message; a page that needs the error template wraps content in `ProblemCard`). Add `tone: "default" \| "danger"` for danger zones. |
| `StatusChip` | ui | Keep: the **one chip** for status. |
| `Table` | ui | Keep, desktop only; `EntityList` switches to cards below `md`. |
| `severity` helpers | ui | Keep; importable only by ui and patterns (S2 already). |
| `EntityHeader` | patterns | Keep. Change: `level: 1 \| 2` (the title's heading level; T3 passes 1), `status` (a `Verdict`), `live` slot, `actions`, inline-editable title; its links move to the `Link` primitive (accent rule). |
| `EntityList` | patterns | Keep. Change: cards below `md`; selection and bulk actions. |
| `EntityPage` | patterns | Becomes `templates/object-page` (T3) with `tabs`; `EmptyState` moves to its own pattern. |
| `FactRow`, `FactGroup`, `Note` | patterns | Keep, **for Details only**: truth kinds are evidence, not everyday labels. |
| `FocusFilter` | patterns | Keep; its links and selected pill move to primitives (accent rule). |
| `HealthBadge` | patterns | Keep: renders any `Verdict`. Live Frame health reaches it as the domain's branded verdict ([§6](#6-status-and-severity)). |
| `LinkToOwner`, `OwnerLinks` | patterns | Keep: the read-only line for P1 ("Now on · next off 23:00, set by Schedule › Power lane"). |

### Primitives for deliveries 1a–1c (`src/ui`)

```ts
// Tabs — one tab per concern; Base UI Tabs' controlled API. The caller keeps the value (in the address);
// only the selected panel is mounted.
interface TabsProps { label: string; tabs: readonly { value: string; label: string }[]; value: string; onValueChange(value: string): void; children: React.ReactNode }

// Inline and Stack — layout primitives so patterns and domain components never write flex classes.
// Inline: controls side by side, wrapping on a narrow screen. Stack: blocks one under the other, evenly spaced.
// With a label, either becomes a named group.
interface InlineProps { label?: string; children: React.ReactNode }
interface StackProps { label?: string; children: React.ReactNode }

// Split — content and its trailing action on one line ("Show ›", a "More" menu, a checkbox, Close, Undo): the
// action never shrinks and never breaks inside a word, even under a container that wraps text anywhere; when the line
// is too narrow for both, the action moves to a row of its own, at the end. Every strip and card header with an action
// is a Split (EditorPage's and FitBoard's lines, MediaCard's header, Sheet's title, UndoToast).
interface SplitProps { children: React.ReactNode; action: React.ReactNode; as?: "div" | "span"; align?: "center" | "start" }

// Field — owns label, help and error wiring for one control (aria-describedby). `notes` are short facts on one
// line under the help (a setting's default, where it acts) that also describe the control. Its context gives the
// control its id, its describedby and the label's id (`labelId`), so a control that is a group is named by the label.
interface FieldProps { label: string; help?: string; error?: string; notes?: readonly string[]; children: React.ReactElement }

// Slider — shows its value and unit as text (or `format`'s words, e.g. a display's level "46 % (117 of 255)"); commits on release, previews on drag; follows the pointer while held whatever the caller does with a preview.
interface SliderProps { value: number; min: number; max: number; step: number; unit: string; format?(v: number): string; onPreview?(v: number): void; onCommit(v: number): void; disabled?: boolean }

// SegmentedControl — 2 to 5 exclusive choices (rotation, nudge step, fit). Alone it shows and carries its `label`;
// inside a Field the Field's label names the group (aria-labelledby) and its help describes it, and `label` is unused.
interface SegmentedControlProps<V extends string> { label: string; value: V; options: readonly { value: V; label: string }[]; onChange(v: V): void }

// Switch — an on/off setting that applies at once.
interface SwitchProps { checked: boolean; onChange(checked: boolean): void; disabled?: boolean }

// Sheet — bottom sheet below md, side sheet above; focus trapped; Escape closes unless busy.
interface SheetProps { open: boolean; onOpenChange(open: boolean): void; title: string; busy?: boolean; children: React.ReactNode }

// Toast — transient, one action, role="status", never takes focus; pauses while hovered or focused.
interface ToastProps { message: string; action?: { label: string; onAction(): void }; seconds: number }

// Select — one choice from a list too long for SegmentedControl (the waiting Pi in Replace with…). Field owns its label.
interface SelectProps<V extends string> { value: V | null; options: readonly { value: V; label: string; hint?: string }[]; onChange(v: V): void; placeholder?: string; disabled?: boolean }

// NumberInput — a number with its unit, for corner pixels, the rotation angle, the Frame display profile and Scene settings in days or percent. Field owns its
// label and error; arrow keys step by `step`, and the value commits on blur or Enter.
interface NumberInputProps { value: number | null; unit: string; min?: number; max?: number; step?: number; onCommit(v: number): void; disabled?: boolean }

// Tag — a neutral small label ("On the display", "Photo Wall (software)"); never a severity colour.
interface TagProps { children: string }
```

Later primitives: `Link` (accent text; 1a, with the accent rule), `Menu` (overflow actions, 1a header on phone), `Skeleton` (1a, templates' loading), `Input` (3b-i, sign-in), `Combobox` (4), `Icon` (3b-i).

### Patterns for deliveries 1a–1c (`src/patterns`)

#### SettingRow

The one anatomy for a setting; one discriminated union so an equipment setting cannot omit where it acts (P2). Prior art: Polaris SettingToggle; iOS Settings rows.

```ts
type ActsOn = "display-hardware" | "pi";   // "On the display" · "On the Pi"; picture settings are grouped by section instead (0022)
type SettingRowProps = SettingRowBase & (
  | { kind: "equipment"; belongsTo: "frame" | "display"; actsOn: ActsOn }
  | { kind: "content"; inherited?: { from: OwnerLink; onOverride(): void } }   // Scene / Photo source / Frame override
  | { kind: "house" }
);
interface SettingRowBase {
  label: string;
  help?: string;                  // one plain sentence
  control: React.ReactElement;    // an unlabelled ui control: the row's Field labels it, and help, actsOn and
                                  // defaultLabel are the Field's help and notes
  defaultLabel?: string;          // "Default: 1.5 s"
  onReset?(): void;               // shown only when the value differs from the default
  state: "idle" | "saving" | "saved" | "error";
  error?: string;
}
```

Stories: `EquipmentOnDisplay`, `EquipmentPictureAdjustment`, `ChangedFromDefault`, `Inherited`, `Overridden`, `Saving`, `Error`, `Disabled`. A11y: the label names the control; help and `actsOn` join `aria-describedby`; Reset says what it resets to.

#### EmptyState

```ts
interface EmptyStateProps { title: string; body: string; action?: React.ReactNode }
```

Says what will be here and the one next step. Prior art: Polaris EmptyState. Stories: `WithAction`, `NoAction`.

#### ProblemCard

The [error template](#the-error-template) as a type. Prior art: GOV.UK error summary; Atlassian section message.

```ts
type ProblemAction = { label: string; onAction(): void } | { label: string; href: string };
type ProblemCardProps = ProblemBase & (
  | { scope: "live"; subject: string; since: string; action: ProblemAction }   // a Frame or Pi problem; since = Central's receive time
  | { scope: "central"; action: ProblemAction }                                // template-wide: "Central did not answer"
  | { scope: "setup"; subject?: string; action: ProblemAction }                // an unfinished setup step: no "since"
  | { scope: "action"; subject?: string; action?: ProblemAction }              // a request just made was refused ("The Pi refused the new corners")
);
interface ProblemBase {
  verdict: Verdict;           // severity + plain words; a live Frame problem passes the domain's status verdict
  what: string;               // plain cause naming the failing part
  doing: string | null;       // "Photo Wall retried 3 times."; null when nothing is automatic
  details?: React.ReactNode;  // FactRows inside Disclosure
  variant?: "card" | "inline" | "banner";   // banner = house-wide (Central or network down)
}
```

A value refused where it is typed (out of range, a duplicate name) is not a `ProblemCard`: it is the control's `Field` `error`, next to the value. A refused request (`scope: "action"`) needs no button of its own when trying the same control again is the fix; every other scope must offer one.

Stories: `NotShowing`, `NeedsALook`, `CantTell`, `SetupTodo`, `CentralUnreachable`, `RefusedAction`, `Inline`, `HouseBanner`, `WithDetails`. A11y: `role="alert"` only for a problem that arrives while the page is open; otherwise a region with a heading.

#### AckBadge

Whether the Pi has acknowledged a requested change. Two states only; a missing acknowledgement past the action's deadline is a `ProblemCard` inline, not a badge state. Prior art: "Saving… / Saved" indicators; [U9](requirements.md#failure-visibility-and-recovery).

```ts
type AckBadgeProps =
  | { state: "requested" }                      // "Previewing — waiting for the Pi"
  | { state: "acknowledged"; at: string };      // "Presented by the Pi · 21:04:07"
```

`at` is Central's `presented_at` for that revision, written HH:MM:SS by the console's one time formatter (`timeWords.js`) in the browser's time zone; when the House timezone arrives (2b) the same formatter switches to it ([units and times](#units-times-and-numbers)).

Stories: `Requested`, `Acknowledged`. A11y: `role="status"`, polite; announces only the change to acknowledged.

#### LivePreviewEditor

The live device editor: a tab of T3 for anything that changes what a physical display shows (Position, Picture).

```ts
interface LivePreviewEditorProps {
  label: string;                 // the region's name ("Position of Living room left")
  latestRevision: number;
  ack: { revision: number; at: string } | null;   // the newest revision the Pi acknowledged presenting
  dirty: boolean;
  onDone(): void;
  onRevert(): void;
  controls: React.ReactNode;     // QuadEditor + NudgePad (Position), or picture sliders
  mirror?: React.ReactNode;      // a scaled outline of the output
  busy?: boolean;                // a Done or Revert is in flight
  status?: string;               // the words shown while no preview session runs ("Not previewing. Move a corner to start.")
  problem?: ProblemCardProps;    // inline, e.g. no acknowledgement by the deadline
  notes?: readonly React.ReactNode[];   // one-line notes, e.g. changes reverted after the editor was left
  unavailable?: { reason: string; action?: React.ReactNode };   // unbound (with the binding picker), or Pi offline
}
```

- **Done** is enabled only when `ack?.revision === latestRevision` (P3); pages cannot pass an enabled flag.
- **Leaving with changes** opens the `LeaveGuard` dialog (Keep or Revert).
- **The tab is hidden** (another tab, phone locked): the draft stays in the page and resumes when the tab is shown again; nothing is reverted.
- **The page is left or closed:** the console stops holding the preview session, Central lets it expire and the Pi returns to the last kept values ([U9](requirements.md#failure-visibility-and-recovery): the diagnostic "expires or is disabled"). The next visit shows a note: "Your unsaved changes were reverted at 21:10 because the editor was closed."
- **The bottom bar** (AckBadge, Done, Revert) is sticky to the bottom of the screen only below `md`; above it, it sits under the controls.
- Stories: `Clean`, `NoSession`, `Requested`, `Acknowledged`, `Busy`, `NoAckProblem`, `Unbound`, `PiOffline`, `RevertedNote`, `Phone`. A11y: Done's disabled reason is visible text; the nudge pad has arrow-key bindings and labelled buttons.

Prior art: Apple HIG display arrangement; projector keystone menus.

#### QuadEditor and NudgePad

The Position controls, as patterns (they know geometry, not Frames). Prior art: projector keystone menus; macOS Displays arrangement.

```ts
type Point = readonly [number, number];
// QuadEditor — four draggable corners and four draggable edges over an outline of the output, at its aspect.
// An edge (its midpoint handle or the line) moves its two corners together by the drag's step (owner,
// 2026-10-12: "i want to be able to drag "edges" of a frame position and not just the corners"). No crop
// edges since 1c (Trim edges removed): the corners set every visible edge.
type QuadPart = { kind: "corner" | "edge"; index: number };  // edge i runs from corner i to the next
interface QuadEditorProps {
  label: string;
  corners: readonly Point[];                        // four, in output coordinates
  aspect: number;
  selected: QuadPart | null; onSelect(part: QuadPart | null): void;  // null: the whole quad
  onCorners(corners: Point[]): void;                // a drag's proposed corners; the caller may refuse them
  onArrow(dx: number, dy: number): void;            // arrow keys on the selected corner or edge
  disabled?: boolean;
}
// NudgePad — four labelled arrow buttons; each press is one step, the caller decides its size (1, 10 or 50 px).
interface NudgePadProps { label: string; subject: string; onNudge(dx: number, dy: number): void; disabled?: boolean }
```

Stories: `QuadEditor/Default`, `/CornerSelected`, `/EdgeSelected`, `/Rotated`, `/Disabled`; `NudgePad/Default`, `/Disabled`. A11y: every corner and edge is a focusable, named handle that focus selects, and is in the Move list; each NudgePad button names its direction and subject ("Move up: top-left corner").

#### Dialog guards

```ts
// LeaveGuard — leaving a live editor with unsaved changes. Plain, not dangerous: both choices are undoable.
// Keep waits for the Pi's acknowledgement like Done: keepBlocked disables it, with the reason shown.
interface LeaveGuardProps { open: boolean; onKeep(): void; onRevert(): void; onStay(): void; keepBlocked?: { reason: string }; busy?: boolean }

// ConfirmDangerous — only for what cannot be undone. The button repeats verb + object ("Retire Pi pw-3f2a").
interface ConfirmDangerousProps { open: boolean; title: string; consequence: string; confirmLabel: string; preview?: React.ReactNode; onConfirm(): Promise<void>; onCancel(): void }
```

Stories: `LeaveGuard/Default`, `/KeepBlocked`, `/Busy`; `ConfirmDangerous/Default`, `/WithPreview`, `/Busy`, `/Error`. Prior art: GitHub danger zone; Apple HIG destructive actions.

#### UndoToast

```ts
interface UndoToastProps { message: string; onUndo(): void }   // Toast with seconds: 10
```

Prior art: Material snackbar with action. Stories: `Default`, `Undone`.

#### QuickActionSheet

A short, frequent action from anywhere: What → Where → How long, each with a default pre-selected; one primary button naming the outcome.

```ts
interface QuickActionSheetProps {
  open: boolean; onOpenChange(open: boolean): void;
  title: string;                                            // "Show now", "Copy picture settings"
  steps: readonly { label: string; control: React.ReactNode }[];   // at most three
  primary: { label: string; onAction(): Promise<void> };    // "Show for 1 hour"
  busy?: boolean; error?: ProblemCardProps;                 // the sheet stays open on error
}
```

Use for Show now, Pause, Copy to other Frames, Replace with…. More than three steps is a T4 Editor. Stories: `ShowNow`, `CopyToFrames`, `Busy`, `Error`, `Phone`. Prior art: Material 3 bottom sheet; Apple HIG action sheets.

#### TargetPicker

Frames and Groups, every current Frame pre-selected **explicitly**, with "All Frames, including ones added later" as a separate choice ([design §12](operator-console-design.md#12-where-a-power-users-expectation-meets-a-written-requirement)).

```ts
type Target = { kind: "frames"; frames: readonly string[] } | { kind: "group"; group: string } | { kind: "all-frames" };
interface TargetPickerProps { frames: readonly { key: string; name: string; room: string }[]; groups: readonly { key: string; name: string }[]; value: Target; onChange(t: Target): void; exclude?: readonly string[] }
```

Stories: `Default`, `GroupChosen`, `AllFramesChosen`, `ExcludesThisFrame`.

### Members for delivery 4's photo pages (follow-ups D1)

| Member | Layer | Purpose | Stories |
|---|---|---|---|
| `MultiToggle` | ui | Two to seven choices (a week's days) any of which can be on together (Base UI ToggleGroup); each shows its state, not by the accent alone: "on" or "off" in words, or `stateWords` where on and off would read as display power (Night off: "✓" and nothing); `required` keeps one on | Default, AllOn, InField, Disabled |
| `Sheet` | ui | The sheet of §2: from the bottom on a phone, from the side above `md`, above the page's sticky bars | Default |
| `Menu` | ui | Overflow actions behind a "More" button named for what it acts on ("More actions for garden") | Default |
| `EditorPage` | patterns | T4: form and preview side by side from `lg` (the preview sticky), stacked from `md`, a count line opening the preview in a `Sheet` on a phone; the `SaveBar` last | Default |
| `SaveBar` | patterns | An editor's Save and Discard, kept at the bottom of the view, with the refusal beside them | Default, Refused |
| `UndoToast` | patterns | As specified above; the count pauses while hovered or focused, and `onExpire` makes the action final (a Source's Delete is sent then) | Default |
| `MediaGrid` | patterns | Thumbnails in justified rows, each at its photo's own ratio (Immich's timeline), never a square crop, since a photo's shape is what a Frame judges it by; "Show more" | Default, WithMore, ReadingMore |
| `FitBoard` | patterns | How a selection of photos fits the Frames, composed for its moment (follow-ups C1 and two design reviews; owner, 2026-10-11: "allow me to preview the content selections and transient Frame compatibility"; "preview is an idea, rather than a specific widget. Because what is being previewed is context dependent"). Its `lead` says what leads: `shapes` (a Source being edited: the selection's `ShapeMap` first, then the Frames it can feed), `wall` (a Scene's Photos step: its Frames side by side in a `WallStrip`, each Frame's counts and reasons under "Each Frame"), `crop` (right under a crop limit being set: the shapes each Frame takes at it, then each Frame's count), `frame` (a Frame's own page, one board per Scene: the Frame's tile and the photos it would show lead, then its counts) or `frames` (the rows, the `ShapeMap` under **Why**). A Frame nothing fits (`alarm`, "Nothing fits") or with too few photos to change (`notice`, "Few photos": fewer than two, or fewer than the Frames sharing the selection, as a photo never shows on two Frames at once) says so **once**, where the Frame is drawn (its tile on a wall, else its row), on a `StatusChip` with one plain sentence and its action; never a separate warning card. `named={false}` on a Frame's own page. Sizes `full`, `compact`, `strip` (Review, Show now: one line and the Frames side by side, small, opening the full board in a `Sheet`; the line over a strip is a neutral count, "Fits 1 of 2 Frames", or with one Frame the selection's count, as each tile says its own alert) and `line` (a `full` board is its line below `md`, a `wall` and a Frame's own page keeping a strip of small tiles); the last answer stays, dimmed under a thin line, while a newer one is read, and greyed as Can't tell when a read fails. It judges nothing: `src/domain/photo-fit.ts` is the one home of its words (every sentence, the "Judged at …" line from the host's `JudgedAt`, and the photo sheet's "Plays from" clause per Frame; each starts with a capital, a Frame's name turned inside the sentence) and its read (Central's one analysis, `POST /v1/operator/photo-fit/analysis`) | Ready, Partial, NoneFit, Reading, Loading, Empty, CantTell, Error at each size; SourceEditor, SceneWall, PlaybackCrop, ReviewStrip, FramePageRow, FramePageNoneFit |
| `WallStrip` (+ `FrameFitTile`) | patterns | A selection's Frames side by side (follow-ups review): each a `FrameFitTile` at its Frame output's shape, filled with a different photo it would show (the answer's leads are distinct, as the Planner's picks are), its name, "N of M fit", and its alert (chip, sentence and action; the chip alone when small). `FrameFitTile` is also the Scene's Frames step's tile beside each checkbox | Wall, AllFit, Small, Faded, PickerTile |
| `FrameFitRow` | patterns | One Frame of a Fit board: its tile at its output's shape filled with what it would show (`FrameTile`; left to the strip under a wall), name and ratio (left out on the Frame's own page), "N of M fit", a bar of fits and refusals in neutral patterns (never a severity colour) with its legend, its alert, at `full` photos cropped as it crops them (beside a large tile in the `pictures` layout), "Why N don't fit" with each reason's photos, the Frame output it is judged at in pixels under **Details** (or, not reported yet, "Judged at hall's planned 9:16 shape; hall hasn't reported its output yet."), never the Display's mode | Ready, NoneFit, FewPhotos, Unmeasured, Compact, Draft, Unnamed, UnderWall, Pictures |
| `ShapeMap` | patterns | The selection's photo ratios as bars on a log axis (9:16 and 16:9 either side of 1:1), and under them each Frame's band of ratios at the crop limit judged; the accent only on a focused Frame's band; the same figures as a table under **Details**; ratios as numbers, never orientation words | Default, OneFrameFocused, Empty |
| `CropThumb` (+ `FrameTile`) | patterns | A photo at its own ratio with the Frame's window drawn where Central put it: the kept part clear, the cut-away dimmed (Immich's crop tool look), a window the Frame would not show dashed; it opens the photo sheet | Fits, RefusedFace, RefusedShape, Video, Openable, Tiles |
| `MediaCard` (+ `MediaCardAdd`, `MediaCardList`) | patterns | T2's card for a thing made of photos: a four-photo mosaic, its name, its count, what it holds, its status chip and More; or, in a form, a choice by checkbox or radio; "+ New …" as the list's last card | Default, Empty, Chosen, List |

### Members for Power and the week (2a, 2b)

Built in [#86](https://github.com/mcurcio/photo-wall/pull/86). Every time they show is a house-time string Central worked out ([0021](decisions/0021-power-timetable-and-repeating-schedules.md)); none works out a time or reads the browser's zone.

| Member | Layer | Purpose | States |
|---|---|---|---|
| `WeekCalendar` | patterns | Seven house days, Monday first, as lanes of bands on a time axis, a now line, Previous / Next week; every band has an accessible name and is also a text list. It takes lanes of plain bands, so Home Assistant holds (3) and several walls (6) add data, not components. Replaces the planned `TimelineLane`. Drawn at its box's own width (text never scales); its bands use the status tokens `--color-band-*`, never the accent | Default, Held, Empty, Phone |
| `PowerLane` | domain | The week page's Power lane over the content calendar: solid bands while displays should be on, grey Night off, hatched holds labelled with who set them ("Held off · console") each with **Resume schedule** (#91) | NightOff, Held, Phone |
| `HoldBanner` | domain | The hold in force above the week: what it holds, until when (one row per group of Frames that end together, each Frame at its own next change), and **Resume schedule** (the planned `OverrideBanner`'s `"power-hold"`) | HeldOff, HeldOnUntilResumed, HeldEachUntilItsOwnChange |
| `HoldLastsControl` | domain | **A hold lasts** (#90) beside All off and All on: until each Frame's next change, or until I resume; saved when chosen, for holds pressed from then on | (in Pages/Week) |
| `NightOffEditor` | domain | Night off's Rules: off and on as a clock time or sunrise/sunset ± minutes (#80, #81; sun choices disabled with "Set the house location" linking to Settings › House while no location is set), days, which Frames (#82: every current Frame ticked, or "All Frames"), "Turn displays off" or "Show black" (#83); a note while Night off waits for the house's timezone (Settings › House never saved) | Default, SunAndChosenFrames, WaitsForZone |
| `FrameWhy` | domain | Why is this playing? on the Frame page's Overview (E6): the winner and what it beat ("Evenings is playing; it beat the Default Scene (lower priority)", "… admitted earlier", "… kept out by a protected Scene") and the power line ("Resting: Night off until 07:00", "Held off by the console until 23:00", "Display didn't answer", never Resting, "Turning off" until the Pi carries the off out, and "Not heard from the Pi") | Resting, DidNotAnswer, NotHeard, Unreachable, AdmittedEarlier, KeptOut, TurningOff |
| `FramePageHeader` | pages | The Frame page's header: its name (Rename), its status chip (Resting when Central says so and nothing else is wrong: a failure beats Resting), its wall when the house has more than one | Showing, Resting, RestingPhone |

### Members for the print look

Built on `feat/print-look` ([0023](decisions/0023-print-look.md)). Each says what a setting does in the owner's words and never works out a picture itself: the Pi and the Player draw it.

| Member | Layer | Purpose | States |
|---|---|---|---|
| `FrameMat` | domain | The Frame's **Mat** on the Position tab, below the corners and their exact values (its own concern, inside the same live editor): Depth (a `Slider` 0–20 mm in 0.5, read "2.5 mm"; millimetres, not the house's [units](#units-times-and-numbers), since a mat is a few millimetres deep), then one `FactGroup` per light, "Light 1" to "Light 4" (Comes from, Steepness, Softness, Strength, Colour, each a `Slider` with its help, and "Remove light N"), "Add a light" (disabled at four, "A mat takes up to 4 lights."), and "No lights: no shadow." or "Depth 0.0 mm: no shadow." when it casts none. It takes a draft and `onChange`, so it rides Position's live session (B4). Its ranges are `contracts/models.py`'s, pinned by `tests/test_console_print_look_ranges.py`; the stories taller than the view are screenshot whole (tag `full-page`) | NoLights, OneLight, Window, Full, NoDepth, Disabled |
| `PhotoWallSettings` (+ `LutSettings`) | domain | The Picture tab's Photo Wall (software) section ([console design §5](operator-console-design.md#5-the-frame-page)): the four of 0022, then Saturation, Soft highlights (noting "acts only above 100 % brightness", the Pi's rule, stated and never decided by the console), Black floor ("2.0 %", help "How light black is, as a share of white light"), and the LUT `Select` (help "Makes photos look printed on one paper"; as wide as its longest name; None and each LUT on the list, "No LUTs on the list yet" while it is empty, with a `LinkToOwner` "Add a paper profile in Settings › LUTs") with LUT amount once one is chosen and, while the Pi fetches it, "The sliders show at once; the LUT joins when the Pi has it, and Done waits for it". Its own stories, since the whole tab's screenshot ends above it | PhotoWallSection, PhotoWallPrintLook, PhotoWallSoftHighlightsIdle, PhotoWallNoSession, LutNone, LutNoneListed, LutWaiting, LutShown |
| `LutsSettings` | domain | Settings › LUTs (`#/luts`): the LUTs list as an `EntityList` (Name, Added, Used by: the Displays that use it, or "No Display"; Remove per row, confirmed in a `Dialog`), "Add a paper profile" as a `FileChooser` (an ICC file from the paper's maker), and one live note with what the last Add or Remove came to: "Added …", or the refusal in plain words (not a colour profile, a colour profile but not for a printer and paper, larger than 8 MB; Remove: the Displays that use it, named). The page reads the list on open and after each Add or Remove, **not by push** (an exception to [Live updates](#live-updates)): a LUT chosen on another tab shows in Used by the next time the page opens, and a Remove Central refuses for it names the Displays | Listed, Empty, Adding, AddRefused, RemoveRefused, Phone |
| `FileChooser` | ui | A button that opens the browser's file chooser: the native file input, hidden from sight inside a label drawn as a `Button`, whose ring shows its focus; it calls back once with the file chosen and empties itself, so the same file can be chosen again | Outline, Primary, Disabled |

### Members for configuration snapshots

Built on `feat/configuration-snapshots` ([0025](decisions/0025-configuration-snapshots.md)). The tabs' words are the [console design §5](operator-console-design.md#5-the-frame-page); each row's time is its entry's `at` (Central's clock), never the browser's, in the console's usual [time words](#units-times-and-numbers).

| Member | Layer | Purpose | States |
|---|---|---|---|
| `HistoryList` | patterns | A titled history of changes, newest first, grouped by day. Props: `title`, `intro`, `notice?` (a line above the days), `days` (each a `label` and its `rows`: `id`, `time`, `what`, `notKept`, `summary`, `details` as label and value pairs, `restore` as `disabled` with an optional `reason`, `session?`), `empty`, `busy` (a skeleton with `aria-busy` while nothing is read yet), `problem?` (a node above the days), `restoring` (the row being restored: every Restore waits), `onRestore(id)`, `onOlder?` ("Show older"). A row not kept is dashed and muted, never a severity colour; rows not kept from one session follow one another as one run: its newest shows and "Show N earlier tries from this session" opens the rest; a Restore reason every disabled row shares is said once above the days. `changedSummary` is every history's one summary rule: what differs from the row below, all of it for the oldest shown | Empty, Loading, Entries, Notice, RestoreDisabled, Restoring, Problem, Phone, SessionRun |
| `PictureHistory` | domain | The Picture tab's History: a Display's picture entries as `HistoryList` rows (each kind in words, display settings named from the tab's own read), Restore into the live picture session and what did not come back | Kept, NotKept, Unrecorded, Skipped, NoSession |
| `PositionHistory` | domain | The Position tab's History: a Frame's Position entries (fit, rotation, mat, lights; corners under Details), Restore into the live draft, and why an entry made for another screen size or display cannot be restored | Kept, NotKept, OtherProfile, OtherDisplay, NoSession |

### Later patterns (props written when their delivery starts)

| Pattern | Purpose | Delivery |
|---|---|---|
| `OverrideBanner` | Something overriding the plan, with an explicit `kind`: `"power-hold"` ("Off — by Home Assistant (Away mode) · **Resume schedule**"), `"takeover"` (Show now: countdown and **Back to normal**) or `"pause"` ("Paused until 22:04 · **Carry on**") | 2a (power-hold); 3b-i (takeover, pause) |
| `TimelineLane` | Built in 2a and 2b as `WeekCalendar` (above) | 2a |
| `SaveBar` (the Schedule's pairs) | Built in 2b as Save (from the next window) / Save and apply now on the editor's bar of delivery 4 | 2b |
| `WallMap`, `WallTile` | Frames to scale; a tile shows the status, what the Pi is presenting and its actions; read-only on Home, editable in T6 | 3b-i (status and actions); 3b-ii (what the Pi presents); 6 (T6) |
| `IncludeExcludePicker` | Photo source filters paired with excludes | 4 |
| `Checklist` | Steps from real checks: T7's body and a settings area's live checks | 3 (Integrations); 6 (T7) |
| `StepProgress` | Long actions across the network ("Reboot requested → started → back online") | 3b-i (G3); 7 (G6) |
| `AppShell` | Rail on desktop, bottom tabs on phone, house word in the top bar | 3b-i |
| `LogView` | A Pi's log, following live | 7 |

## 5. Interaction rules

### Save models

The models are defined here; [§8](#8-concept--ux-translation) alone assigns one to each concept.

| Model | Behaviour | Patterns | Prior art |
|---|---|---|---|
| **Live** (then Done or Revert) | Every change goes to the real display at once; **Done** unlocks only when the Pi acknowledges presenting the latest change; **Revert** restores the last kept values; leaving asks Keep or Revert; the session expires if the console goes away | `LivePreviewEditor`, `AckBadge`, `LeaveGuard` | Apple HIG Displays |
| **Autosave** (with Undo) | Saved on change (debounced for text, on release for sliders); `UndoToast` for 10 s; the row shows saving → saved | `SettingRow`, `UndoToast` | iOS Settings; Material snackbar |
| **Save** (from the next start) | **Save** applies from the next start ([requirements](requirements.md#live-media-compatibility-and-preparation)); **Save and apply now** restarts what is playing | `SaveBar` | Polaris contextual save bar |
| **Schedule** | Applies to future blocks; if the current block changes, choose **Change now** or **From the next block** | `SaveBar` | Calendar "this event / following events" |
| **Action** | A command that acts at once (Skip, Show now, All off, Restart). Reversible actions offer Undo or their own way back (Back to normal, Resume schedule); irreversible ones confirm first; device actions show `AckBadge` until acknowledged | `Button`, `QuickActionSheet`, `UndoToast`, `ConfirmDangerous` | Gmail undo send |

Live changes to equipment do not conflict with the next-start rule, which covers authored content only.

### Confirmation and destructive actions

- Confirm only what cannot be undone (Retire a Pi, Restore a backup, Remove from Home Assistant, Delete a Scene in use). Everything undoable acts at once and offers Undo: never confirm and also offer Undo.
- Irreversible actions sit in a danger zone at the bottom of the last tab or area, red only there.
- The confirm button repeats verb and object; the dialog states the consequence in one sentence. Restore shows a preview of what changes first (GOV.UK "check answers").

### Feedback timing

| Wait | Feedback |
|---|---|
| < 100 ms | none |
| 100 ms – 1 s | the control shows busy (button spinner, row "Saving…") |
| 1 – 10 s | `Skeleton` in the region, or `AckBadge` "waiting for the Pi" |
| > 10 s or across a reboot | `StepProgress` with a time per step; the user may leave and come back |
| No answer by the action's deadline | the error template, with Retry |

(Nielsen's response-time limits.)

### Live updates

- Every view updates by push; there is no **Refresh** button anywhere (design §9). One exception, recorded where it applies: the LUTs list (Settings › LUTs and the Picture tab's LUT picker) is read when the view opens and after each Add or Remove (`LutsSettings`), since it changes only when the owner adds or removes a paper profile. A view whose push stream drops keeps its data greyed with "Can't tell since …", not a reload prompt.
- An update never moves what the user is pointing at: a list re-sorts only when the pointer leaves it or after an action.

### Optimistic and confirmed state

- **Optimistic** (new value at once, rolled back with an inline `ProblemCard` on error): anything Central alone decides: names, toggles, Never show, Groups.
- **Acknowledged** (shown as requested until each device acknowledges): anything a Pi or display must do: Position, Picture, Copy to other Frames (per Frame), power Test, Restart, Skip. The console never shows a request as done (P4).

## 6. Status and severity

The six status words are the domain's verdict on a Frame's live health ([0019 decision 8](decisions/0019-first-principles-console.md#decisions-proposed)); 0018's severity scale stays as it is. This section is their one home.

| Word | Meaning | Severity | Order (worst first) |
|---|---|---|---|
| **Not showing** | Should be showing and isn't. One fix offered. | `alarm` | 1 |
| **Can't tell** | No report since [time]. No guessing ([U6](requirements.md#failure-visibility-and-recovery)). | `unknown` | 2 |
| **Needs a look** | Playing, but degraded: old photos, Immich unreachable, storage low. Can wait. | `notice` | 3 |
| **Getting ready** | Starting, updating or downloading. It will show photos by itself. | `notice` | 4 |
| **Showing** | The Pi reports it is presenting what was planned. (It cannot see a display someone switched off with the remote.) | `ok` | 5 |
| **Resting** | Dark on purpose: Night off, paused, all off. Nothing wrong. | `ok` | 5 |

- **The house word** is the worst Frame word by this order; when every Frame is Showing or Resting it is **All good** (design §9). This order is a decision of this document: it ranks Can't tell above Needs a look because a silent Frame may be dark. It is the domain's order over words and differs on purpose from `WORST_FIRST` in `tokens.ts`, which orders severities for lists.
- **Where it lives in code:** a `StatusWord` union, an exhaustive `Record<StatusWord, Severity>`, the order and the house roll-up in `src/domain` (one module, like `domain/host-health.tsx`'s `hostVerdict`). It emits the existing `Verdict` branded as a status verdict, which only that module can construct, so a domain component that shows live Frame health cannot pass free text. The design layer stays free of Photo Wall concepts.
- **Setup is not status:** the scale's `todo` marks unfinished setup (no Pi yet, Position not checked); it appears as a setup step or a `todo` chip with its own words, never as one of the six. A Frame's chip is drawn the same way everywhere (`domain/frame-health-chip.ts`): "Needs a Player" is `todo`, a Pi enrolled with no report yet is **Getting ready**.
- **One chip:** `StatusChip` (through `HealthBadge`) is the only thing that draws a severity colour on text; rows may add a severity bar for `notice` and `alarm` only.
- **One colour meaning:** severity colours mean status; the accent means "you can act here" and never status; there is no decorative colour (S3).
- **Never by colour alone:** the word is always present (WCAG 1.4.1).
- **Other domains** (Pis, Photo library, Integrations) speak their own plain words through `Verdict` on the same scale and chip.

## 7. Content and voice

### Glossary

The object labels (House, Wall, Frame, Display, Pi, Photo source, Scene, Schedule) and their domain terms are the [object model](operator-console-design.md#2-the-object-model); new terms are [design §3](operator-console-design.md#3-new-terms). This table adds the words for verbs, states and parts.

| UI says | Domain term (code) | Notes |
|---|---|---|
| Playing now | Run (active) | |
| Show now | Activation request (overlay Run) | |
| Fed by Pi pw-3f2a · HDMI 1 | Binding of a Player Output | |
| HDMI port | Output | |
| Position | Calibration (geometry) | |
| Picture | Calibration (photometric), display settings | |
| What the Pi is presenting | Display Host's reported presentation | |
| Presented by the Pi | Display Host's presentation acknowledgement ([U9](requirements.md#failure-visibility-and-recovery)) | never "on the display": the panel's pixels are a separate observation |
| Photo app | Player app | "Restart photo app" |
| Pi software | Node release, base, app | "Pi software 1.4.2" |
| Last heard 21:04 | last report received by Central | Central's receive time |
| Details | evidence, truth kind, layer | the only place internal words appear |
| History | picture history (by Display), Position history (by Frame) ([0025](decisions/0025-configuration-snapshots.md)) | never "log", "audit" or "snapshot" on a tab; "configuration snapshot" is the owner's word for one Frame at one time, read by the agent |
| Tried, not kept | a sent draft entry (`sent-draft`) | values that reached the wall while tuning and were never kept by Done |
| Put back | a sent back entry (`sent-back`) | the wall returned to the kept values after Revert or leaving the tab |
| Kept (Done) | a kept entry Done wrote (`kept-done`, `kept-saved`) | the values Done kept |
| Kept (Copy from another display) | a kept picture entry Copy wrote (`kept-copied`) | another display's values copied here |
| Kept (screen size changed) | a kept Position entry a profile change wrote (`kept-profile-changed`) | the Frame's screen size changed; its position was kept as it was |
| Changed outside Photo Wall | a kept unrecorded entry (`kept-unrecorded`) | the stored values differed from the last kept change: changed at an unknown time before it |
| Kept values sent again | a sent kept entry (`sent-kept`) | Central sent the kept values anew (after Done, Copy, a display newly seen); "sent", never "shown": the Pi's answer is "Presented by the Pi" |
| Earlier tries from this session | a run of not-kept rows of one tuning session | the newest shows; the earlier ones open on request |
| Restore | a Restore of a history entry into the session's draft | loads the editor; Done still keeps, Revert still puts back |

### Forbidden words

Outside **Details**, in any string a user reads: JSX text, string props, and the label strings domain helpers build in `.ts` files.

| Kind | Words | Checked by |
|---|---|---|
| **Bare words** (never correct in a label) | Node API, NATS, bus, hub, lease, epoch, effect gate, boot offer, qualification, Host Management, App Lifecycle, Display Host, Actuator, Activation, AssetSource, Panel, Binding, Installation, calibration, draft, on screen, Refresh, Submit | lint, with an allow list for proper names |
| **Qualified** (wrong only in one sense) | link (as a state), claimed / reported / derived (as labels), Program, Run, Player (for the box), Output, Surface, "is visible" (as a claim about the display), OK (as a button), Error (as a heading alone) | review |

### The error template

What is wrong in plain words, naming the failing part (display, Pi, photo app, network, Central, Immich) — since when — what Photo Wall is doing about it — one button. **Details ›** holds the layer, where the evidence came from and its time. `ProblemCard` is the template as a type; a template-wide failure has no subject, a setup problem no "since".

> "Living room left: Not showing since 21:04. The Pi is fine but the display stopped answering on HDMI. Photo Wall retried 3 times. [Check the display is on this input] Details ›"

Never claim what the display lights up, and never offer a fix the system cannot perform.

### Buttons and verbs

- Verb plus object where the object is not obvious: "Restart photo app", "Reboot Pi", "Show for 1 hour", "Resume schedule", "Replace with…". The ellipsis means "asks for more before acting".
- Sentence case. No "Click here", "Submit" or bare "OK".
- Pairs: Done / Revert (Live); Save / Save and apply now (Save); Change now / From the next block (Schedule); Undo (Autosave, Action); Keep / Revert (LeaveGuard).

### Units, times and numbers

| Kind | Format |
|---|---|
| Clock time | One formatter (`timeWords.js`), on the House's zone and clock ([settings catalogue](operator-console-design.md#11-settings-catalogue) #2, #5): "21:04" or "9:04 pm", no zone abbreviation. Until the shell's first read of Settings › House answers (a moment after sign-in, or while Central does not answer it), times are in the browser's zone with its abbreviation ("21:04 BST"), so a time is never shown in a zone it does not name. Acknowledgement times carry seconds ("21:04:07") |
| Since / last heard | Absolute beyond an hour ("since 21:04", "since Tue 09:12"); "3 min ago" only within the hour; always Central's receive time, never a Pi's clock |
| Check and contact times | `timeWords.js` `sinceTime`: the weekday and a 24-hour time, "Checked Sun 03:39", "Needs a look since Sun 03:39"; no zone abbreviation, no seconds; a date instead of the weekday only when a served read time says it is more than six days old. One time per page: the pool's line carries no second copy of its check's time |
| Durations | "30 s", "1 h", "1 h 30 min" |
| Lengths | House units (cm or in), one decimal at most |
| A mat's depth | Millimetres, one decimal ("2.5 mm"): a mat is a few millimetres deep, below what the house's units show (`FrameMat`; [catalogue](operator-console-design.md) #151) |
| Counts | Locale grouping ("41,203 photos"); exact, never "many" |
| Percent, colour | "50 %"; "6500 K"; gamma "2.2" |
| Pixels | Only in Position ("nudge 10 px") and Details |

## 8. Concept → UX translation

Every journey step in the [roadmap](roadmap.md#the-journey), one row per home (page › section), exactly one template per row ("T3 + LivePreviewEditor" means the pattern inside that template). **Save** is the one place a concept's [save model](#save-models) is assigned. A step's status today and its delivery are the roadmap's.

| Step | Home | Template | Patterns | Save | Status and feedback | Empty / error |
|---|---|---|---|---|---|---|
| A1 Install Central | Setup checklist › Server | T7 | Checklist, ProblemCard | none | each step green from a real check | "Network boot files not staged" with the fix |
| A2 Sign in | Sign-in page | exception (no template) | Field, Button | Action | lands on Home | wrong password inline, without saying which part was wrong |
| A2 Owner account | Settings › People and access | T8 | SettingRow | Autosave | "Signed in as …" | none |
| A3 Connect Immich | Settings › Photo library | T8 | SettingRow, Field (the key a password field, then "Key ending …1234"), Button (Check now), ProblemCard | Save (Save checks at once) | "41,203 photos · 2,110 videos · 212 albums · 96 people" (Immich's totals, archived photos included) and the Immich user; Connected, Checking, Paste the address and key, or Needs a look; the deployment's connection reads "Set by your deployment. Change its address or key there." with Check now and no Change, Paste a new key or Remove | EmptyState "Connect your photo library: paste the address of your Immich and an API key below."; a ProblemCard per problem with its one fix, for example "Immich rejected the API key." → Paste a new key (on the deployment's connection: "Set a new API key in your deployment; Photo Wall uses it from its next start." → Check again), each saying "Photos keep playing." |
| A4 House basics | Settings › House (`#/house`; built in 2b: timezone, location, units, clock format) | T8 | SettingRow, SegmentedControl | Autosave | none | the seeded zone is Central's own and not confirmed (Night off waits until the house is saved once); when the browser's differs it is offered ("Use this browser's timezone (…)?"), never applied by itself; an unknown zone refused in plain words |
| A5 Ready for Pis | Setup checklist › Ready for Pis | T7 | Checklist, ProblemCard | none | "Network boot OK · release 1.4.2 ready" | the problem names the piece (DHCP, files, release) |
| B1 Pi appears | Pis › New Pis waiting | T2 | EntityList, HealthBadge | none | the display shows its code; "Waiting · seen 21:04" | EmptyState "Plug a Pi into a display and power"; "2 GB, needs 4 GB" |
| B1 Pi appears | Setup checklist › First Pi | T7 | Checklist | none | done when a Pi is waiting or bound | links to Pis › New Pis waiting |
| B2 Identify | Frame page › header | T3 | Button, AckBadge | Action | requested → acknowledged | no acknowledgement: ProblemCard inline, Retry |
| B3 Name and place | Frame page › header (name; built in 2b) | T3 | EntityHeader (inline title) | Autosave | saved inline | duplicate name: inline error "That name is taken" |
| B3 Name and place | Frames › Wall tab (the wall's name and room label, #92; built in 2b) | T2 | SettingRow, Field | Autosave | saved inline | none |
| B3 Name and place | Frames › wall layout (placement) | T6 | WallTile, Tabs (walls) | Autosave | overlaps flagged | EmptyState "Add your first Frame" |
| B4 Fit the picture | Frame page › Position | T3 + LivePreviewEditor | QuadEditor, NudgePad, SegmentedControl (fit: Cover or Corners; rotation 0/90/180/270), NumberInput and Slider (angle, Cover only), AckBadge, LeaveGuard, FrameMat (the mat's depth and lights, [0023](decisions/0023-print-look.md)), then History as `PositionHistory` on `HistoryList` (each change with its time, Restore into the draft; [0025](decisions/0025-configuration-snapshots.md)) | Live (Restore only fills the draft; Done keeps) | Previewing → Presented by the Pi | unbound → binding picker; Pi offline → disabled with since; expired session note; a Cover Save whose picture would turn against the Frame's orientation: the refusal in plain words beside the angle |
| B5 Picture quality | Frame page › Picture | T3 + LivePreviewEditor | two sections: Display (hardware), a Slider per level the display offers, read as a percent of its range with the display's own count second ("46 % (117 of 255)"; owner, 2026-10-12, picked "Percent, raw count second"), a Select of exactly its named choices per list setting, a read-only line otherwise, each with "As the display has it"; Photo Wall (software) as `PhotoWallSettings`: four Sliders (brightness and contrast 0–150 %), then the print look ([0023](decisions/0023-print-look.md)): Sliders for Saturation, Soft highlights and Black floor, a Select for the LUT and a Slider for its amount; then display tuning ([0024](decisions/0024-display-tuning.md)): a SegmentedControl Test pattern (Off, White, Grey ramp, Black level) with one line under each pattern saying what to do with it ([console design §5](operator-console-design.md#5-the-frame-page); the wording lives in the tab alone), and a SegmentedControl Signal range (Auto, Full, Limited) noting that a change restarts the picture on every display of the Pi; below the live editor, the Power tab's own input SettingRow (B6), saved on change outside Done and Revert ([0022](decisions/0022-picture-hardware-and-software-apart.md) R10); then History as `PictureHistory` on `HistoryList` (each change to the display's picture with its time, Restore into the editor; [0025](decisions/0025-configuration-snapshots.md)) | Live (the input row: Autosave; Restore only fills the draft, Done keeps) | the section says where a setting acts; "Checking what this display offers" until the Pi has asked; Done once the Pi reports the latest hardware values written, the latest software values and test pattern presented and the latest signal range answered | a write the display refuses: the row says "The display did not take it"; a set value changed on the display's buttons: "Changed on the display's buttons" with the display's value, left alone until the next change; a signal range the Pi could not set: the range the display still gets |
| B5 Picture quality | Settings › LUTs (`#/luts`; [§4](#4-component-and-pattern-catalogue) `LutsSettings`) | T8 | EntityList (each LUT: name, added, used by), FileChooser (Add a paper profile), Dialog (Remove, confirmed) | Action (Add, Remove) | the added LUT on the list, named from its profile | EmptyState "Add a paper profile from your paper's maker"; a file that is not a printer paper profile, too large, or a LUT a Display uses: the refusal in plain words, nothing else changes |
| B6 Power | Frame page › Power | T3 | SettingRow, Button (Test), Note (live status, in place of `AckBadge`, whose words are the live editor's), LinkToOwner | Autosave (options), no Undo until `UndoToast` exists; Action (Test) | "Waiting for the Pi" until the Pi answers the change that carried the test, then "Display confirmed off" / "Display confirmed on" / "Picture stopped; the display may sleep" / "Display didn't answer" / "Left on: the display is showing another input"; Pi not linked: "Last heard from the Pi at …" | no method answered (`not-supported`): EmptyState naming the smart-plug option; unbound: "Choose which Pi and HDMI port feeds this Frame", linking to Hardware |
| B7 First photos | Setup checklist › First photos | T7 | Checklist, MediaGrid | none | done when a Frame is Showing | Immich not connected → that step first |
| C1 Wall layout | Frames › wall layout | T6 | WallTile, Field (size in house units) | Autosave | overlaps flagged | EmptyState "Add your first Frame" |
| C2 Rooms and groups | Frames › Groups | T2 | EntityList, TargetPicker | Autosave | member count | rooms make groups by themselves |
| C3 Copy settings | Frame page › Picture | T3 + QuickActionSheet | TargetPicker, AckBadge per Frame | Action + Undo | per Frame: requested → acknowledged | a display that refuses a value: that Frame's row says so |
| C4 Two displays per Pi | Frame page › Hardware | T3 | SettingRow, LinkToOwner (Pi page lists ports read-only) | Autosave | "Fed by Pi pw-3f2a · HDMI 2" | port in use: the picker shows by which Frame |
| C5 Span | Frames › wall layout | T6 | WallTile | Autosave | deferred ([roadmap](roadmap.md#deferred-and-what-that-costs)) | none |
| D1 Pick photos | Photo source editor › Include | T4 (`EditorPage`) | IncludeExcludePicker, MultiToggle, MediaGrid, SaveBar, Sheet | Save | live count and thumbnails at each photo's ratio | "No photos match all of these: …" naming every choice the query makes, each with a Clear (per-choice counts are not built: each would be one more Immich search) |
| D2 Exclude | Photo source editor › Exclude | T4 | IncludeExcludePicker, SettingRow | Save | the pool beside the form is read again from the top | Leave out albums, people and tags (none by default) and Leave out screenshots (on by default); one fixed line, not a switch: "Archived, hidden and locked photos are never included." |
| D3 Photo fit | Scene editor › Photo fit (the flow's Playback step; the flow runs Kind → Frames → Photos → Playback → Review since follow-ups C1) | T4 | SettingRow, Slider (Crop limit, whole percent, its range from Settings › Photo fit; unset follows the house crop limit and says so, Reset follows it again; help: "The most of a photo a Frame may cut away to fill itself. Faces and the interesting part are always kept."), each step previewing its own question from one read: Frames, each Frame a `FrameFitTile` beside its checkbox, its count once the Scene has photos; Photos, the Source cards, then right under them FitBoard led by the wall (the Scene's Frames side by side, each with a different photo; a strip below `md`), then one line of the chosen Sources' actions (Check now, Remove, Manage in Sources; the cards say each check's state); Playback, FitBoard led by the crop limit right under its slider (the shapes each Frame takes, then each Frame's count, its drag answered by shape before it is let go); a strip on Review, its sheet with "Raise the crop limit to N %") | Save | the board | a Frame nothing fits: said once, where it is drawn: "Most of these photos are 3:2 and hall's Frame output is 9:16: add photos closer to 9:16 or raise the crop limit." (before hall reports its output: "hall is planned at 9:16") |
| D3 Photo fit | Frame page › Overview › Photos | T3 | one row per Scene showing photos here: how it relates to now ("Playing now", "Scheduled …", "Not scheduled"), LinkToOwner to the Scene, what this Frame gets from it (FitBoard led by the Frame: its tile and the photos it would show, then its counts, not naming the Frame again; its Frame output in pixels under Details; a small tile of its own below `md`); the Position and Frame profile editors a line judged with the unsaved values ("Judged at its Frame output as you set it, 16:9"), its pixels under the Details of the board it opens | none (read-only) | the board | "No Scene shows photos from a Source on this Frame yet" with "Put content on this Frame" |
| D3 Photo fit | Settings › Photo fit (`#/photo-fit`) | T8 | SettingRow (house) per tuning value, its range Central's (`FitSettingsView.ranges`): Crop limit, Space kept around a face, Smallest face that counts; under Advanced the rest, Crop positions tried under its Details; a compact FitBoard judged with the values as typed | Save ("Each Frame uses it from its next photo; the photos showing now finish their turn.") | the board | a value outside its range: its field's error |
| D4 Order | Scene editor › Order (the flow's Playback step) | T4 | SettingRow, SegmentedControl (Order: Shuffle, Oldest first, Newest first, On this day), NumberInput (Don't repeat within, days; 0 reads "Off"; help: "A photo shown recently waits its turn, but a Frame never goes blank because of it.") | Save | Review lists both | none |
| D5 Timing and transitions | Scene editor › Timing | T4 | SettingRow, Slider | Save | preview plays the timing | none |
| D6 Video | Scene editor › Video | T4 | SettingRow, Slider, Switch | Save | share also shown as a count | no videos match: one-line note |
| D7 Captions | Scene editor › Captions | T4 | SettingRow, SegmentedControl | Save | preview shows a caption | none |
| E1 Weekly routine | Schedule › week page (`#/schedule`), content lanes; the Schedule flow's When step | T5 | WeekCalendar, Sheet, SaveBar | Save (from the next window; **Save and apply now** on a playing Schedule) | now line; block labels; Review lists Central's next windows; "ends next day" shown when the end is at or before the start | only the Default Scene band + "Add a Schedule"; a newer save: reload |
| E2 Night off | Schedule › week page › Power lane › Rules | T5 | WeekCalendar, PowerLane, NightOffEditor | Save (a stale save reloads) | grey band "Off 23:00 – 07:00" | a Frame with no power method: "black screen only" |
| E3 Sunrise and sunset | Schedule › editor (When step) and Power lane › Rules | T5 | SettingRow, Field, NightOffEditor | Save, as E1 and E2 | resolved time beside the rule ("sunset −30 min · 18:42") | no House location → choices disabled, "Set the house location" linking to Settings › House |
| E4 Holidays | Schedule › editor › Date range | T5 | Field, TimelineLane | Schedule | the range overrides blocks visibly | overlaps explained by priority |
| E5 Default content | Schedule › week page › Default Scene | T5 | SettingRow, Select | Action (applies at once; "Nothing" allowed) | the band fills the gaps | none chosen → `todo` chip |
| E6 Why is this playing? | Frame page › Overview (built in 2b; the Power tab reads the same power line; Home tiles in 3b-i) | T3 | FrameWhy, LinkToOwner | none | one sentence: "Evenings is playing; it beat the Default Scene (lower priority)", and the power line "Resting: Night off until 07:00" | "Not heard from the Pi"; a display that did not answer is a power problem, never Resting |
| F1 Glance | Home › map | T1 | WallMap, WallTile, HealthBadge | none | pushed; "What the Pi is presenting" | per-tile Can't tell; house banner if Central unreachable |
| F2 Skip and previous | Home › tile | T1 | WallTile actions, AckBadge | Action | requested → acknowledged | not offered on secured content ([design §12](operator-console-design.md#12-where-a-power-users-expectation-meets-a-written-requirement)) |
| F2 Skip and previous | Frame page › header | T3 | Button, AckBadge | Action | requested → acknowledged | as above |
| F3 Never show | Home › tile | T1 | WallTile actions, UndoToast | Action + Undo | "Won't show again · Undo" | on secured content: from the next start (design §12) |
| F3 Never show | Scenes › Never show list | T2 | MediaGrid, UndoToast | Action + Undo | **Show again** per photo | EmptyState "Nothing hidden from the wall" |
| F4 Show now | Home › Show now (and the phone tab) | T1 + QuickActionSheet | TargetPicker, OverrideBanner (takeover) | Action | banner with countdown and **Back to normal** | nothing matches: error inside the sheet |
| F5 All off/on | Home › quick actions | T1 | Button, UndoToast | Action + Undo | per Frame acknowledged; displays that didn't answer listed by name | none |
| F5 All off/on | Schedule › week page header (All off, All on, A hold lasts) and Power lane (the hold; built in 2a) | T5 | Button, PowerLane, WeekCalendar | Action (Resume schedule) | hatched hold with who and until; a held page shows "Held off until 07:00" | none |
| F6 Phone layout | AppShell | exception (no template) | AppShell, cards, Sheet | n/a | bottom tabs Home · Show now · Schedule · More | no sideways scrolling anywhere |
| F7 Home Assistant | Settings › Integrations | T8 | SettingRow, Checklist (live checks), Button (Test), ConfirmDangerous (Remove) | Autosave; Action (Test, Remove) | "Broker connected → Home Assistant online → 12 devices published → last command 21:04" | "Broker rejected the username/password" |
| F8 Pause | Home › tile (default length in the Show now sheet, #86) | T1 | WallTile actions, OverrideBanner (pause) | Action (its way back: **Carry on**) | the Frame reads Resting; "Paused until 22:04" | Pi didn't acknowledge: ProblemCard inline |
| G1 Health at a glance | Home › house word | T1 | HealthBadge, AppShell | none | the house word ([§6](#6-status-and-severity)) | Can't tell rather than a guess |
| G2 Diagnose | Home › problems | T1 | ProblemCard, Disclosure | none | error template | Details holds the evidence |
| G2 Diagnose | Frame page › Overview | T3 | ProblemCard, Disclosure, FactRow | none | recent problems with times | none |
| G3 Restart | Pis › Pi page | T3 | Button, StepProgress | Action | "Reboot requested → started → back online" | a failed step with its time and Retry |
| G4 Replace a Pi | Frame page › Hardware › Replace with… | T3 + QuickActionSheet | Select (the waiting Pi), UndoToast | Action + Undo | "Living room left is now fed by pw-91c0" | no waiting Pi: "Plug in the new Pi" |
| G4 Replace a Pi | Pis › New Pis waiting | T2 | EntityList, QuickActionSheet ("Which Frame did you plug this into?") | Action + Undo | one tap binds | EmptyState as in B1 |
| G5 Display changed | Frame page › Hardware (prompt) | T3 | ProblemCard (`todo`) with two steps, FactRow | Action (step 1, **Confirm Frame profile**, pre-filled from the new display); step 2 links to Position | Frame not ready until Position is re-checked | a display with no usable serial: "Recognised by make and model on this Frame" ([design §6](operator-console-design.md#6-display-identity)); the display listed no modes: step 1 sends you to Edit Frame profile |
| G5 Display changed | Frame page › Position (re-check) | T3 + LivePreviewEditor | as B4 | Live | Done clears the block | as B4 |
| G6 Updates | Settings › Updates | T8 | SettingRow, StepProgress, ConfirmDangerous | Autosave (choice); Action (Apply) | "1.5.0 downloaded · Apply", then per-Pi progress | download failed: error template |
| G7 Backup and restore | Settings › Backups | T8 | SettingRow, EntityList, ConfirmDangerous (with preview) | Autosave; Action (Restore, confirmed) | "Last backup 03:00 · 2.1 MB" | EmptyState with "Back up now" |
| G8 Logs | Pis › Pi page › Logs | T3 | LogView, SettingRow (log level) | Autosave | following, pushed | Pi offline: last lines kept, Can't tell since |
| G9 Notifications | Settings › Notifications | T8 | SettingRow, Button (Send a test) | Autosave; Action (test) | "Test sent 21:04" | EmptyState "Choose where to be told"; a channel that refused: error template |
| G10 Retire and reset | Pis › Pi page (danger zone: Retire) | T3 | Section (danger), ConfirmDangerous | Action (confirmed) | "pw-3f2a retired; its Frames show nothing until you choose another Pi" | none |
| G10 Retire and reset | Settings › Backups (danger zone: Reset Central) | T8 | Section (danger), ConfirmDangerous (type to confirm, with a backup offered first) | Action (confirmed) | the console returns to the Setup checklist | none |
| G11 Upgrade Central | Settings › Updates (Central's version, read-only) | T8 | HealthBadge, LinkToOwner (to the backup taken before it) | none | "Central 1.6.0 · backup taken 03:00 before the upgrade" | upgrade held because the backup failed: error template |

G10's Reset Central and G11's version line have no home in the [settings catalogue](operator-console-design.md#11-settings-catalogue) or the design's pages yet; the homes above are this document's proposals until the design adds them.

## 9. Enforcement

Strongest guarantee first. "Enforced" holds on the branch today; "Add" is proposed.

| Rule | Guarantee | Status |
|---|---|---|
| Layers import down; the catalogue imports no console model (S1) | test (`tests/test_console_routes_r4.py`) | enforced |
| Pages and domain components do not style (S2) | lint | enforced |
| Colour only from semantic tokens (S3) | lint (+ palette cleared) | enforced |
| Every story renders, passes axe and matches its baseline in dark and light | test (`tests/browser/test_console_catalog_browser.py`) | enforced |
| Catalogue and pages type-check | compile (`npm run typecheck`) | enforced |
| Six words ↔ severity, their order, the house word (P6) | **compile:** exhaustive `Record<StatusWord, Severity>` and the branded status verdict in `src/domain` ([§6](#6-status-and-severity)) | add |
| Done only after the Pi acknowledges (P3) | **compile:** `LivePreviewEditor` derives Done from `ack` and `latestRevision` | add |
| The error template has its parts | **compile:** `ProblemCard` requires `verdict`, `what`, `doing`, `action` (except a refused request), and for `scope: "live"` (a Frame or Pi problem) also `subject` and `since` | add |
| Equipment settings say where they act (P2) | **compile:** `SettingRow`'s discriminated union; `actsOn` a closed union, required for `kind: "equipment"` | add |
| Templates render every state | **compile:** templates take `LoadState<T>`; **test:** each template's stories include `Loading`, `Empty`, `Error`, `CantTell` | add |
| Every page is a template (P7) | **lint:** a custom rule that a `src/pages` file's default export renders a `patterns/templates/*` component at its root; the two listed exceptions carry an inline disable naming §3 | add |
| Accent only for actions (P6) | **lint:** `better-tailwindcss/no-restricted-classes` refusing `(text\|bg\|border\|ring\|outline)-accent*` except in an allow list of primitives (`ui/button`, `ui/disclosure`, `ui/link`, `ui/tabs`, `ui/segmented-control`, `ui/slider`, `ui/switch`, and `patterns/quad-editor` for its draggable handles). `entity-header.tsx:23,31` and `focus-filter.tsx:21,24,32` must move to those primitives first | add |
| Plain words (P5) | **lint:** a rule over JSX text, string props and string literals in `src/domain` and `src/pages` (including `.ts` helpers that build labels) refusing the bare [forbidden words](#forbidden-words), read from one list file, which then becomes the list's home and §7 links to it; qualified words stay review | add |
| Type sizes only from roles | **lint:** clear Tailwind's size scale in `tokens.css` and define five role tokens, so `no-unknown-classes` refuses `text-sm`. Cost: the 25 size classes in `ui` and `patterns` are renamed in one sweep (no visual change if each role keeps today's value). Alternative: keep the scale and rely on S2 (sizes already appear only in `ui` and `patterns`), review-only | add (recommended at the DS sweep) |
| Icons only through `ui/icon` | **lint:** `no-restricted-imports` of the icon package outside `src/ui` | add at 3b-i |
| Every pattern has stories | **test:** every `src/patterns/**/*.tsx` has a `.stories.tsx` | add |
| Phone layout holds (F6) | **test:** template stories also screenshot at a 390 px viewport. Cost: doubles those baselines and their leg time; shard if the leg passes ~4 min | add |
| Every roadmap step has a translation row | **test:** `scripts/check_docs.py` checks each step id in the roadmap tables appears in [§8](#8-concept--ux-translation) | add |
| One home per setting (P1), one concern per tab, qualified words, one primary per view | **review:** the console-ux skill's checklist | review |
| A UI PR shows its change | **review:** every PR that changes the console embeds before and after screenshots in dark and light in its description | review |

Costs: the compile-time members constrain props (a page cannot pass free text as a Frame status); the forbidden-words lint needs an allow list for proper names; the phone screenshots cost CI time.
