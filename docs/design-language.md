# Console design language

**Status:** proposed, 2026-10-09; nothing new here is built. This is the one home for the console's UI rules: principles, page templates, the pattern catalogue, how things save, status and severity, and the words the console uses. It builds on [decision 0018](decisions/0018-console-by-domain-and-design-system.md) (layers, tokens, severity scale, rules S1 to S3, the look) and never contradicts it. **What** each page holds is the [operator console design](operator-console-design.md); **when** it is built is the [roadmap](roadmap.md). The procedure that applies this document to one feature is the project skill `.claude/skills/console-ux/SKILL.md`.

The owner, 2026-10-09 (chat): "This roadmap needs to be matched with a clear design language package, and a design skill that knows how to translate these individual feature concepts into a normalized UX". Earlier the same day: "i continue to be frustrated with how poorly designed the UI is." and "I would encourage focusing on a design language and building a catalog of react primitives rather than hard coding specific pages" ([0018](decisions/0018-console-by-domain-and-design-system.md#the-owners-words)).

Everything below except those quotes is a design choice.

**Contents:** [1 Principles](#1-principles) · [2 Foundations](#2-foundations) · [3 Page templates](#3-page-templates) · [4 Component and pattern catalogue](#4-component-and-pattern-catalogue) · [5 Interaction rules](#5-interaction-rules) · [6 Status and severity](#6-status-and-severity) · [7 Content and voice](#7-content-and-voice) · [8 Concept → UX translation](#8-concept--ux-translation) · [9 Enforcement](#9-enforcement)

## 1. Principles

Each principle has a test; a design or a page that fails the test is wrong, whatever it looks like.

| # | Principle | Test (how a reviewer or a check tells) | Prior art |
|---|---|---|---|
| P1 | **One home per setting.** Every setting and every fact is changed in exactly one place; anywhere else it appears as a read-only line that links to its home. | The [settings catalogue](operator-console-design.md#11-settings-catalogue) names one "Lives on" per setting; no page renders a control for a setting whose home is elsewhere (it renders `LinkToOwner`). | 0018's H1; Home Assistant device pages link to the entity's one settings dialog |
| P2 | **Show where a setting acts.** A control that changes equipment says what it belongs to (this Frame, this display, the house) and where it takes effect (on the display, Photo Wall picture adjustment, Central). | `SettingRow`'s `actsOn` and `belongsTo` are required props for equipment settings ([§4](#settingrow)). | macOS Displays and TV menus name the device; Polaris "explain the consequence" |
| P3 | **Live on the device, then Done or Revert.** A change to a display shows on the real display while you make it; **Done** keeps it and is offered only once the Pi confirms it presents the latest change. | `LivePreviewEditor` disables Done until `ack.state === "confirmed"` for the latest revision ([§4](#liveprevieweditor)). | Apple HIG Display settings (apply live, revert on timeout); [U9](requirements.md#failure-visibility-and-recovery) |
| P4 | **Say what is true; never claim pixels.** Every status names its source and time; what nobody reported is **Can't tell since …**, never a guess. The console says what the Pi reports it presents, never what the display lights up. | Every status carries `since` or a receipt; "on screen" and "is visible" are forbidden words ([§7](#forbidden-words)). | [U6](requirements.md#failure-visibility-and-recovery); Home Assistant's "Unavailable" state |
| P5 | **Plain words.** Labels come from the glossary; internal words appear only under **Details**. | The forbidden-words check ([§9](#9-enforcement)) passes over every UI string in `src/domain` and `src/pages`. | GOV.UK content design: "use plain English" |
| P6 | **One status vocabulary, one colour meaning.** Six status words, drawn on 0018's severity scale; colour means status and nothing else; the accent colour means "you can act here". | `StatusWord` is a typed union mapped to `Severity` once ([§6](#6-status-and-severity)); S3 lint; accent classes only in `ui/button` and links. | Atlassian lozenges (one colour per meaning); Polaris tone |
| P7 | **Every page is a template.** A feature composes one template from [§3](#3-page-templates) out of catalogue patterns; a feature that fits no template is a design question raised before code, not a new layout. | Each page's root is a template pattern ([§9](#9-enforcement)); a page story exists per required state. | GOV.UK patterns ("use a pattern before designing your own"); Polaris page templates |

## 2. Foundations

The values live in [`tokens.css`](../central/console/src/design/tokens.css) and its TypeScript vocabulary [`tokens.ts`](../central/console/src/design/tokens.ts) and nowhere else (0018, S3). This section names the roles; it never copies a value.

| Foundation | Today (tokens.css) | Rule | Gap to close |
|---|---|---|---|
| **Colour roles** | Surfaces (`surface`, `-raised`, `-sunken`, `-input`); text (`text`, `label`, `muted`); lines (`line`, `line-input`); `accent`, `accent-tint`, `on-accent`, `focus`; severity (`ok`, `todo`, `notice`, `alarm`, `unknown`, `on-alarm`); truth kinds; `shadow` | Page = `surface`; a card or tile = `surface-raised`; a well (preview, log) = `surface-sunken`. Severity colours only through `StatusChip`, `HealthBadge` and the severity helpers in `ui/severity.tsx`. Accent only for actions, links, selection and focus. | none |
| **Type scale** | Console Sans (Immich's Google Sans); sizes are Tailwind's defaults (`text-xs` … `text-lg`) used ad hoc in primitives | Five roles: page title, section title, body, label, meta (receipts, units). Pages never choose a size (S2 already forbids `className` there). | Add the five roles as `--text-*` tokens and clear Tailwind's size scale, as the palette is cleared |
| **Spacing and density** | `--spacing: 4px` (Tailwind step); `--spacing-row: 8px` | One density. Touch targets at least 44 px on phone (WCAG 2.5.5, Apple HIG); table rows keep `spacing-row` on desktop. A density toggle stays deferred (0018). | none |
| **Radius** | `button`, `input`, `card`, `pill` | Chips are `pill`; tiles and cards are `card`. | none |
| **Motion** | none | 150 ms ease-out for state changes (sheet in, toast in, tab change); no motion that carries meaning alone; `prefers-reduced-motion` turns it off. Live previews never animate (the display is the preview). | Add `--duration-fast` and `--ease-standard` tokens |
| **Icons** | none | Recommended: **Material Design Icons (`@mdi/js`)**, the set Immich's web app uses, so the console feels related to Immich; behind one `ui/icon` primitive that takes an icon path and a required label or `decorative`. Cost: shadcn's generated components import lucide, so each copied component swaps its few icons by hand. Alternative: lucide (shadcn's default, no glue), less like Immich. Icons never replace a word on an action except Skip/Previous on a tile, which keep an accessible name. | Open question 1 in the PR |
| **Dark and light** | Dark first; light overrides the same tokens; follows the system unless `data-scheme` is set | Every member works in both; the catalogue walker already screenshots and runs axe in both schemes for every story. | none |
| **Breakpoints** | Tailwind's defaults (not cleared) | One switch: below `md` (768 px) is **phone**: bottom tabs, tables become cards, sheets come from the bottom, editors stack preview above controls. At `md` and up: the rail, tables, side sheets. No other breakpoint changes structure. | Name the switch as a token so templates and patterns share it |

## 3. Page templates

The normalized shapes. Every page is one of these; the [decision table](#choosing-a-template) picks it. Navigation (the rail, the phone tabs, which section a page sits in) is the [operator console design §4](operator-console-design.md#4-navigation).

**Required states, all templates.** `Loading` (skeletons in the layout's shape, never a spinner over a blank page), `Empty` (an `EmptyState` with the one next step), `Error` (a `ProblemCard`: Central could not answer, with Retry), and `Can't tell` (data is stale or a source is silent: the last values stay, greyed, with "Can't tell since 21:04"). A template's stories cover all four.

### Choosing a template

| The concept is mainly… | Template |
|---|---|
| "Is it OK and what is on the wall right now?" | [T1 Home](#t1-home) |
| Many things of one kind, to find one or act on several | [T2 Object list](#t2-object-list) |
| One thing the user manages (a Frame, a Pi, a Scene's summary) | [T3 Object page](#t3-object-page) |
| Authoring content with many fields and a preview (Scene, Photo source) | [T4 Editor](#t4-editor) |
| Adjusting a physical display until it looks right | [T5 Live device editor](#t5-live-device-editor) (a tab inside T3) |
| Something over time: blocks, lanes, holds, "why now" | [T6 Schedule and timeline](#t6-schedule-and-timeline) |
| A short, frequent action from anywhere, especially a phone | [T7 Quick-action sheet](#t7-quick-action-sheet) |
| First-run steps that each turn green from a real check | [T8 Setup checklist](#t8-setup-checklist) |
| House-wide configuration of one area (Photo library, Integrations, Backups) | [T9 Settings area page](#t9-settings-area-page) |

### T1 Home

- **Anatomy:** house `StatusWord` + one sentence; `ProblemCard`s pinned on top (worst first); `HoldBanner`s for active takeovers and holds; the `WallMap` per Wall; a now/next strip; primary quick actions (Show now, All off/on).
- **Use** for the console's landing only. **Not** for setup tasks (they go to T8) or settings.
- **States:** Empty = no Frame yet → the Setup checklist's next step. Error = whole Central unreachable → one house banner replaces per-tile alerts. Can't tell = per tile.
- **Phone:** one column; map tiles two across; quick actions in the bottom tabs ([design §4](operator-console-design.md#4-navigation)).
- **Prior art:** Home Assistant dashboard tiles; Atlassian Statuspage header; signage "screen wall" views.

### T2 Object list

- **Anatomy:** `EntityHeader` (title, add action); optional `FocusFilter`; `EntityList` grouped, worst first; bulk actions on selection; each row opens its T3 page.
- **Use** for Frames, Pis, Scenes, Photo sources, Schedules, the Never show list (as a `MediaGrid`). **Not** when there is only ever one of the thing (use T3 or T9).
- **States:** Empty = `EmptyState` with the create action; Error; Can't tell per row (status column).
- **Phone:** each row becomes a card: name, status chip, one secondary fact; no sideways scrolling, ever.
- **Prior art:** Polaris IndexTable (cards on small screens); GOV.UK summary list.

### T3 Object page

- **Anatomy:** `EntityHeader` (editable name, where it sits, `StatusWord`, live view where one exists, two or three header actions) then `Tabs`; **one tab = one concern** (the Frame page's tabs are [design §5](operator-console-design.md#5-the-frame-page)). The first tab is Overview (read-only). Destructive actions sit in a danger zone at the bottom of the last tab.
- **Use** for anything the user manages as a thing. **Not** for authoring content (T4).
- **States:** each tab has its own; an unbound Frame shows every tab with the binding picker where equipment is needed (design §5).
- **Phone:** tabs scroll sideways as a strip; header collapses to name + status; header actions move to an overflow menu except the first.
- **Prior art:** Home Assistant device page (controls, sensors, configuration, diagnostics); signage "screen settings" pages.

### T4 Editor

- **Anatomy:** `EntityHeader` (name, kind); form `SettingsSection`s in a fixed order of concern; a live preview (`MediaGrid` with count, or a preview on a chosen Frame) beside it; a `SaveBar` with **Save** and **Save and apply now** ([§5](#5-interaction-rules)).
- **Use** for Scenes and Photo sources. **Not** for equipment (T5) or single toggles (autosave in T3/T9).
- **States:** Loading preview separately from the form; Empty preview = "No photos match" with the filter that removed most; Error per section.
- **Phone:** preview collapses to a count bar that opens the grid; sections are disclosures.
- **Prior art:** Polaris contextual save bar; Immich's album and search filters; GOV.UK "check answers" for the summary.

### T5 Live device editor

- **Anatomy:** a `LivePreviewEditor`: the display shows a test pattern; controls (corner handles, nudge pad, sliders) on the page; an `AckBadge` (Previewing — waiting for the Pi → Presented by the Pi); **Done** and **Revert**; no session timeout while open.
- **Use** for Frame page › Position and › Picture: anything changing what a physical display shows. **Not** for settings with no visible effect (autosave).
- **States:** Unbound → binding picker; Pi offline → editor disabled, "Can't reach Pi pw-3f2a since 21:04"; ack timeout → "The Pi didn't confirm" with Retry and Revert; leaving with unsaved changes → `ConfirmDangerous` (Keep or Revert).
- **Phone:** controls stack under a small mirror of the frame; big arrow buttons and corner handles (design §4).
- **Prior art:** Apple HIG display arrangement; projector keystone menus; [U9](requirements.md#failure-visibility-and-recovery).

### T6 Schedule and timeline

- **Anatomy:** a time axis with a now line; `TimelineLane`s (Power lane on top, then content lanes per Frame or Group); blocks open their editor in a sheet; `HoldBanner` for holds with **Resume schedule**; "Why is this playing?" opens an explanation sheet.
- **Use** for the week (Schedule page) and a Frame's day (Frame page › Overview). **Not** for a single start/end pair (a form field).
- **States:** Empty = only the Default Scene band, with "Add a Schedule"; Can't tell = lanes hatched after the last report.
- **Phone:** one day at a time, vertical time axis, swipe days.
- **Prior art:** Google Calendar week view; thermostat schedules with hold and resume (Nest, ecobee).

### T7 Quick-action sheet

- **Anatomy:** a `QuickActionSheet`: What → Where → How long, each with a sensible default pre-selected; one primary button naming the outcome ("Show for 1 hour"); an `UndoToast` after.
- **Use** for Show now, Pause, All off/on with options, Replace with…. **Not** for anything that needs more than three choices (T4).
- **States:** Loading choices inline; Error inline in the sheet, the sheet stays open.
- **Phone:** bottom sheet with drag handle; desktop: side sheet.
- **Prior art:** Material 3 bottom sheet; Apple HIG action sheets.

### T8 Setup checklist

- **Anatomy:** a `Checklist` of `ChecklistStep`s: title, one-line why, status from a real check (never a click), the action that fixes it; the next step expanded.
- **Use** for first-run and the "New Pis waiting" step list. **Not** for ongoing health (T1).
- **States:** each step: Not started, Checking, Done (with when), Problem (`ProblemCard` inline); the whole list hides itself when done (design §9).
- **Phone:** same; one step open at a time.
- **Prior art:** GOV.UK task list; Polaris setup guide.

### T9 Settings area page

- **Anatomy:** title; `SettingsSection`s of `SettingRow`s; a connection area has a live check list ("Broker connected → Home Assistant online → 12 devices published") and a **Test** button with a counted result; a danger zone last.
- **Use** for every Settings › area. **Not** for settings of one Frame or one Scene (they live on that object).
- **States:** Not set up (an `EmptyState` naming what it gives you), Testing, Connected (with counts), Problem (error template).
- **Phone:** one column, rows full width.
- **Prior art:** Immich's administration settings; Home Assistant integration config entries; Polaris annotated sections.

## 4. Component and pattern catalogue

Layers and import rules are 0018's. A **primitive** (`src/ui`) knows no Photo Wall concept; a **pattern** (`src/patterns`) takes plain data and links; a **domain component** (`src/domain`) turns one domain's model into pattern props. New primitives come from shadcn on Base UI where one exists.

### Existing members

| Member | Layer | Keep / change |
|---|---|---|
| `Button` | ui | Keep. Add `size: "default" \| "touch"` (44 px on phone) and an optional leading icon. One `primary` per view; `danger` only inside `ConfirmDangerous` and danger zones. |
| `Dialog` | ui | Keep (its in-flight rule stands). Base of `ConfirmDangerous`. |
| `Disclosure` | ui | Keep. The one way to show **Details ›**. |
| `Section` | ui | Keep (it is an error boundary). Its fallback renders the error template. |
| `StatusChip` | ui | Keep: the **one chip** for status. Nothing else draws a severity colour on a pill. |
| `Table` | ui | Keep, desktop only; `EntityList` switches to cards below `md`. |
| `severity` helpers | ui | Keep; importable only by ui and patterns (S2 lint already). |
| `EntityHeader` | patterns | Keep the name. Change: add `status` (a `StatusWord` badge), `live` (the live view slot), `actions`, and an inline-editable title. |
| `EntityList` | patterns | Keep. Change: card layout below `md`; selection and bulk actions. |
| `EntityPage` | patterns | Keep. Change: takes `tabs` (the `Tabs` primitive) for T3; `EmptyState` moves out to its own pattern. |
| `FactRow`, `FactGroup`, `Note` | patterns | Keep, **for Details only**: truth kinds are evidence, not everyday labels. |
| `FocusFilter` | patterns | Keep (Pis list, focus in the address). |
| `HealthBadge` | patterns | Keep. Change: for live health it takes a `StatusWord` and `since`, not free text; free text stays for Details. |
| `LinkToOwner`, `OwnerLinks` | patterns | Keep. The read-only line for P1 ("Now on · next off 23:00, set by Schedule › Power lane"). |

### New primitives (`src/ui`, from shadcn on Base UI)

| Primitive | Purpose | Notes |
|---|---|---|
| `Tabs` | One tab per concern on T3 | Address-synced (a tab is a URL); arrow-key roving focus (WAI-ARIA tabs) |
| `Field`, `Input`, `Select`, `Switch`, `Slider`, `SegmentedControl`, `Combobox` | Form controls | `Field` owns label, help and error wiring (`aria-describedby`); `Slider` shows its value and unit |
| `Sheet` | Bottom sheet (phone) / side sheet (desktop) | Base UI Dialog; focus trapped, Escape closes unless busy |
| `Toast` | Transient message with one action | Base UI Toast; `role="status"`, never steals focus |
| `Menu` | Overflow actions | Base UI Menu |
| `Skeleton` | Loading shape | `aria-busy` on the region, not on each skeleton |
| `Tag` | Neutral small label ("On the display", "This display") | Never a severity colour (that is `StatusChip`) |
| `Icon` | One icon from the chosen set | `label` required unless `decorative` |

### New patterns (`src/patterns`)

Every pattern has a story file; the catalogue walker renders each story in dark and light, runs axe and diffs a baseline (no separate dark/light stories are written).

#### SettingRow

The one anatomy for a setting. Prior art: Polaris SettingToggle; Home Assistant entity settings; iOS Settings rows.

```ts
interface SettingRowProps {
  label: string;
  help?: string;                       // one plain sentence
  control: React.ReactNode;            // a ui control
  belongsTo?: "frame" | "display" | "house" | "scene" | "source"; // required for equipment
  actsOn?: string;                     // "On the display" | "Photo Wall picture adjustment" | "Central"
  defaultValue?: string;               // shown as "Default: 50 %"
  onReset?: () => void;                // "Reset to default", only when value ≠ default
  inherited?: { from: OwnerLink };     // "Uses the Scene's setting" + link
  onOverride?: () => void;             // turns an inherited value into this object's own
  state?: "idle" | "saving" | "saved" | "error";
  error?: string;
}
```

States: default, changed from default, inherited, overridden, saving, saved, error, disabled with reason. Stories: `Default`, `ChangedFromDefault`, `Inherited`, `Overridden`, `ActsOnDisplay`, `ActsInSoftware`, `Saving`, `Error`, `Disabled`. A11y: the label is the control's accessible name; help and `actsOn` join `aria-describedby`; Reset says what it resets to.

#### SettingsSection

A titled group of `SettingRow`s with an optional one-line intro and a danger-zone variant. Prior art: Polaris annotated layout. `{ title: string; intro?: string; tone?: "default" | "danger"; children }`. Stories: `Default`, `Danger`. A11y: a `<section>` with a heading.

#### EmptyState

`{ title: string; body: string; action?: React.ReactNode; illustration?: "none" | "frame" | "photos" }`. Says what will be here and the one next step. Prior art: Polaris EmptyState. Stories: `WithAction`, `NoAction`. Moves out of `entity-page.tsx`.

#### ProblemCard

The error template ([§7](#the-error-template)) as a type, so a problem cannot be shown without its parts. Prior art: GOV.UK error summary; Atlassian section message.

```ts
interface ProblemCardProps {
  subject: string;            // "Living room left"
  word: StatusWord;           // "Not showing"
  since: string;              // already formatted, Central's receive time
  what: string;               // plain cause naming the failing part
  doing: string | null;       // "Photo Wall retried 3 times."; null only if nothing is automatic
  action: { label: string; onAction: () => void } | { label: string; href: string };
  details?: React.ReactNode;  // FactRows under Disclosure
  variant?: "card" | "inline" | "banner"; // banner = house-wide (Central or network down)
}
```

Stories: `NotShowing`, `NeedsALook`, `CantTell`, `Inline`, `HouseBanner`, `WithDetails`. A11y: `role="alert"` only for a problem that arrives while the page is open; otherwise a region with a heading.

#### StatusWordBadge

`HealthBadge` specialised for live health: `{ word: StatusWord; since?: string }`, severity looked up from the one mapping ([§6](#6-status-and-severity)). Stories: one per word. A11y: the word is text; colour never carries it alone (WCAG 1.4.1).

#### AckBadge

Shows whether the Pi has confirmed it presents the latest change. Prior art: Google Docs "Saving… / Saved"; [U9](requirements.md#failure-visibility-and-recovery).

```ts
type Ack =
  | { state: "idle" }
  | { state: "sending"; revision: number }
  | { state: "previewing"; revision: number }          // "Previewing — waiting for the Pi"
  | { state: "confirmed"; revision: number; at: string } // "Presented by the Pi · 21:04:07"
  | { state: "failed"; revision: number; reason: string }; // "The Pi didn't confirm"
```

Stories: one per state. A11y: `role="status"`, polite; announces only `confirmed` and `failed`.

#### LivePreviewEditor

The T5 shell: `{ ack: Ack; latestRevision: number; onDone(): void; onRevert(): void; controls: React.ReactNode; mirror?: React.ReactNode; dirty: boolean }`. **Done is disabled unless `ack.state === "confirmed" && ack.revision === latestRevision`** (P3). The corner-handle canvas and nudge pad are domain components passed in as `controls`. Stories: `Clean`, `Previewing`, `Confirmed`, `Failed`, `PiOffline`, `Phone`. A11y: Done's disabled reason is visible text, not only a tooltip; the nudge pad has arrow-key bindings and labelled buttons.

#### HoldBanner

`{ what: string; by: string; reason?: string; until?: string; onResume(): void }` → "Off — by Home Assistant (Away mode) · until you turn it off · **Resume schedule**". Prior art: thermostat hold banners (ecobee, Nest). Stories: `ByPerson`, `ByHomeAssistant`, `WithUntil`, `NoEnd`. A11y: Resume names what resumes.

#### TimelineLane

`{ label: string; start: Date; end: Date; now?: Date; blocks: readonly { key: string; start: Date; end: Date; label: string; kind: "on" | "off" | "content" | "hold" | "default"; href?: string }[] }`. Hatched = hold; grey = Night off; solid = on or content. Stories: `PowerLane`, `ContentLane`, `WithHold`, `CrossesMidnight`, `Empty`, `CantTell`. A11y: a list of blocks with times as text, not only geometry; kinds differ by pattern as well as colour.

#### WallMap and WallTile

`WallMap { wall: string; unit: "cm" | "in"; tiles: readonly WallTileProps[]; editable?: boolean }`; `WallTile { title: string; href: string; word: StatusWord; since?: string; presenting?: { src: string; alt: string; label: string } | null; actions?: readonly TileAction[] }`. Tiles are laid out to scale; on Home they show what the Pi presents, labelled "What the Pi is presenting"; in the Frames section the map is an editor. Prior art: Home Assistant picture-elements; signage wall views. Stories: `Showing`, `Resting`, `NotShowing`, `CantTell`, `Unbound`, `Editable`, `Phone`. A11y: tiles are also a list in reading order; drag has keyboard nudge.

#### UndoToast

`{ message: string; onUndo(): void; seconds?: number /* 10 */ }` on the `Toast` primitive. Prior art: Material snackbar with action; Gmail undo. Stories: `Default`, `Undone`. A11y: Undo reachable by keyboard; the toast pauses while focused or hovered.

#### QuickActionSheet

`{ title: string; steps: readonly { label: string; control: React.ReactNode }[]; primary: { label: string; onAction(): void }; busy?: boolean }` on `Sheet`. Stories: `ShowNow`, `Pause`, `AllOff`, `Busy`, `Error`, `Phone`.

#### TargetPicker

Frames and Groups, every current Frame pre-selected **explicitly**, with "All Frames (including ones added later)" as a separate choice ([design §12](operator-console-design.md#12-where-a-power-users-expectation-meets-a-written-requirement)). `{ frames: readonly {key: string; name: string; room: string}[]; groups: readonly {key: string; name: string}[]; value: Target; onChange(t: Target): void }`. Used by Show now, Scene › Where, the power schedule, Copy to other Frames. Stories: `Default`, `GroupChosen`, `AllFramesChosen`.

#### IncludeExcludePicker and MediaGrid

`IncludeExcludePicker { facets: readonly Facet[]; include: Selection; exclude: Selection; onChange(...) }`: chips for albums, people, places, dates, favourites, tags, each pairable with an exclude. `MediaGrid { count: number | null; items: readonly { key: string; src: string; alt: string }[]; onItemAction?(key, action): void; empty: React.ReactNode }`. Prior art: Immich search filters and timeline grid. Stories: `Counting`, `Results`, `NoMatch`, `NeverShowList`.

#### Checklist and ChecklistStep

`ChecklistStep { title: string; why: string; status: "todo" | "checking" | "done" | "problem"; doneAt?: string; action?: React.ReactNode; problem?: ProblemCardProps }`; `todo` uses the scale's `todo` severity. Prior art: GOV.UK task list. Stories: `AllTodo`, `InProgress`, `WithProblem`, `AllDone`.

#### StepProgress

Long actions that cross the network: `{ steps: readonly { label: string; state: "waiting" | "active" | "done" | "failed"; at?: string }[] }` ("Reboot requested → Reboot started → Back online"). Stories: `Running`, `Done`, `Failed`. A11y: `role="status"` announces step changes.

#### ConfirmDangerous

`{ title: string; consequence: string; confirmLabel: string; typeToConfirm?: string; onConfirm(): Promise<void> }` on `Dialog`: the verb repeats in the button ("Retire Pi pw-3f2a"), never "OK". Prior art: GitHub danger zone; Apple HIG destructive actions. Stories: `Default`, `TypeToConfirm`, `Busy`, `Error`.

#### SaveBar

`{ dirty: boolean; onSave(): void; onSaveAndApply?(): void; onDiscard(): void; busy?: boolean; applyChoice?: { now: string; later: string } }` for T4 and schedule edits ("Change now" / "From the next block"). Prior art: Polaris contextual save bar. Stories: `Clean`, `Dirty`, `WithApplyNow`, `ScheduleChoice`, `Busy`.

#### AppShell

The rail (desktop) and bottom tabs (phone), the house status in the top bar, and the Setup checklist entry until finished; items come from [design §4](operator-console-design.md#4-navigation). Prior art: Material 3 navigation rail and bar. Stories: `Desktop`, `Phone`, `WithChecklist`.

#### LogView

`{ lines: readonly { at: string; level: "info" | "warning" | "error"; text: string }[]; follow: boolean; onFollowChange(b: boolean): void; download?: string }` in a `surface-sunken` well. Stories: `Following`, `Paused`, `Empty`. A11y: a `log` role region.

## 5. Interaction rules

### Save models

Four models; each concept uses exactly one ([§8](#8-concept--ux-translation) says which). The "Used for" column is the one statement of which concept saves how.

| Model | Used for | Behaviour | Pattern | Prior art |
|---|---|---|---|---|
| **Live, then Done or Revert** | Frame page › Position, › Picture | Every change goes to the real display at once; **Done** unlocks only when the Pi confirms the latest change ([U9](requirements.md#failure-visibility-and-recovery)); **Revert** restores the last kept values; leaving asks Keep or Revert | `LivePreviewEditor`, `AckBadge` | Apple HIG Displays |
| **Autosave with Undo** | Toggles and values: Power options, names, Photo fit and Hardware tabs, Settings pages, Groups, the Never show list | Saved on change (debounced for text, on release for sliders); `UndoToast` for 10 s; the row shows saving → saved | `SettingRow`, `UndoToast` | iOS Settings; Material snackbar |
| **Explicit Save, from the next start** | Scenes, Photo sources | **Save** applies from the next start ([requirements](requirements.md#live-media-compatibility-and-preparation)); **Save and apply now** restarts what is playing | `SaveBar` | Polaris contextual save bar |
| **Schedule change** | Schedules, the Power lane | Applies to future blocks; if the current block changes, choose **Change now** or **From the next block** | `SaveBar` with `applyChoice` | Calendar "this event / following events" |

Position, picture and power are equipment settings, so applying them live does not conflict with the next-start rule, which covers authored content only.

### Confirmation and destructive actions

- Confirm only what cannot be undone (Retire a Pi, Restore a backup, Remove from Home Assistant, Delete a Scene in use). Everything undoable acts at once and offers Undo: never confirm and also offer Undo.
- Destructive actions sit in a **danger zone** at the bottom of the last tab or area, red only there (`danger` button inside `ConfirmDangerous`).
- The confirm button repeats the verb and object; the dialog states the consequence in one sentence ("Its 2 Frames will show nothing until you choose another Pi").
- Restore shows a preview of what changes before the confirm (GOV.UK "check answers").

### Feedback timing

| Wait | Feedback |
|---|---|
| < 100 ms | none |
| 100 ms – 1 s | the control shows busy (button spinner, row "Saving…") |
| 1 – 10 s | the region shows `Skeleton` or `AckBadge` "waiting for the Pi" |
| > 10 s or crosses a reboot | `StepProgress` with a time per step; the user may leave and come back |
| No answer within the action's own deadline | the error template, with Retry |

(Nielsen's response-time limits.)

### Live updates

- Every view updates by push; there is no **Refresh** button anywhere (design §9). A view whose push stream drops shows its data greyed with "Can't tell since …", not a reload prompt.
- An update never moves what the user is pointing at: a list re-sorts only when the pointer leaves it or after an action.

### Optimistic and confirmed state

- **Optimistic** (show the new value at once, roll back on error with a `ProblemCard` inline): names, toggles, Never show, Groups: anything Central alone decides.
- **Confirmed** (show "requested" until the device answers): anything a Pi or display must do: Position, Picture, power Test, Restart, Reboot, Skip. The console never shows a requested state as done (P4).

## 6. Status and severity

The six status words are the domain's verdict on a Frame's live health; the pattern layer draws them on 0018's severity scale, which stays as it is ([0019 decision 8](decisions/0019-first-principles-console.md#decisions-proposed)). This table is their one home.

| Word | Meaning | Severity |
|---|---|---|
| **Showing** | The Pi reports it is presenting what was planned. (It cannot see a display someone switched off with the remote.) | `ok` |
| **Resting** | Dark on purpose: Night off, paused, all off. Nothing wrong. | `ok` |
| **Getting ready** | Starting, updating or downloading. It will show photos by itself. | `notice` |
| **Needs a look** | Playing, but degraded: old photos, Immich unreachable, storage low. Can wait. | `notice` |
| **Not showing** | Should be showing and isn't. One fix offered. | `alarm` |
| **Can't tell** | No report since [time]. No guessing ([U6](requirements.md#failure-visibility-and-recovery)). | `unknown` |

- **Roll-up:** the house word is the worst Frame word by `WORST_FIRST` in `tokens.ts`; Resting never counts as a problem.
- **Setup is not status:** the scale's fifth level, `todo`, marks unfinished setup (no Pi yet, Position not checked); it is shown as a `ChecklistStep` or a `todo` chip with its own words, never as one of the six.
- **One chip:** `StatusChip` (through `StatusWordBadge` or `HealthBadge`) is the only thing that draws a severity colour on text. A row may add a severity bar (`severityBar`) for `notice` and `alarm` only.
- **One colour meaning:** severity colours mean status; the accent means "you can act here" and never status; there is no decorative colour (S3).
- **Never by colour alone:** the word is always present (WCAG 1.4.1).
- **Other domains** (Pis, Photo library, Integrations) speak their own plain words but draw them on the same scale with the same chip.

## 7. Content and voice

### Glossary

The object labels (House, Wall, Frame, Display, Pi, Photo source, Scene, Schedule) and their domain terms are the [object model](operator-console-design.md#2-the-object-model); new terms are [design §3](operator-console-design.md#3-new-terms). This table adds the words for verbs, states and parts the console shows.

| UI says | Domain term (code) | Notes |
|---|---|---|
| Playing now | Run (active) | |
| Show now | Activation request (overlay Run) | |
| Fed by Pi pw-3f2a · HDMI 1 | Binding of a Player Output | |
| HDMI port | Output | |
| Position | Calibration (geometry) | never "calibration" in labels |
| Picture | Calibration (photometric), display settings | |
| What the Pi is presenting | Display Host's reported presentation | never "on screen" |
| Photo app | Player app | "Restart photo app" |
| Pi software | Node release, base, app | "Pi software 1.4.2" |
| Last heard 21:04 | last report received by Central | Central's receive time |
| Details | evidence, truth kind, layer | the only place internal words appear |

### Forbidden words

Not in labels, buttons, headings or messages outside **Details** (the list is the source the planned lint reads, [§9](#9-enforcement)): Node API, link (as a state), bus, hub, NATS, lease, epoch, effect gate, claimed, reported (as a label), derived, boot offer, qualification, Host Management, App Lifecycle, Display Host, Actuator, Activation, Program, AssetSource, Panel, Player (for the box), Output, Binding, Surface, Installation, Run, draft, calibration, on screen, is visible (a claim about the display), Refresh, OK (as a button), Submit, Error (as a heading alone).

### The error template

What is wrong in plain words, naming the failing part (display, Pi, photo app, network, Central, Immich) — since when — what Photo Wall is doing about it — one button. **Details ›** holds the layer, where the evidence came from and its time. `ProblemCard` is the template as a type.

> "Living room left: Not showing since 21:04. The Pi is fine but the display stopped answering on HDMI. Photo Wall retried 3 times. [Check the display is on this input] Details ›"

Never claim what the display lights up, and never offer a fix the system cannot perform.

### Buttons and verbs

- A button is a verb plus object where the object is not obvious: "Restart photo app", "Reboot Pi", "Show for 1 hour", "Resume schedule", "Replace with…". The ellipsis means "asks for more before acting".
- Sentence case everywhere. No "Click here", no "Submit", no bare "OK".
- Pairs: Done / Revert (live editors); Save / Save and apply now (editors); Change now / From the next block (schedules); Undo (after autosave).

### Units, times and numbers

| Kind | Format |
|---|---|
| Clock time | House clock format ([settings catalogue](operator-console-design.md#11-settings-catalogue) #5), house timezone: "21:04" or "9:04 pm" |
| Since / last heard | Absolute for anything older than an hour ("since 21:04", "since Tue 09:12"); "3 min ago" only within the hour; times are Central's receive times, never a Pi's clock |
| Durations | "30 s", "1 h", "1 h 30 min" |
| Lengths | House units (cm or in), one decimal at most |
| Counts | Locale grouping ("41,203 photos"); exact, never "many" |
| Percent, colour | "50 %"; "6500 K"; gamma "2.2" |
| Pixels | Only in Position ("nudge 10 px") and Details |

## 8. Concept → UX translation

Every journey step in the [roadmap](roadmap.md#the-journey) mapped to its normalized UX. **Home** is page › section. **Save** uses the [§5](#save-models) names: Live, Autosave, Save, Schedule, or none. A step's status today and its delivery are the roadmap's.

| Step | Template | Home | Patterns | Save | Status and feedback | Empty / error |
|---|---|---|---|---|---|---|
| A1 Install Central | T8 | Setup checklist › Server | Checklist, ChecklistStep, ProblemCard | none | each step green from a real check | "Network boot files not staged" with the fix |
| A2 Sign in | own sign-in page (T9 shape) | Settings › People and access | Field, Button | Save | "Signed in as …" | wrong password inline; never says which part was wrong |
| A3 Connect Immich | T9 | Settings › Photo library | SettingRow, Field, Button (Test), Checklist | Autosave + Test | "41,203 photos, 212 albums, 96 people" | EmptyState "Connect your photo library"; "Immich rejected the API key" |
| A4 House basics | T9 | Settings › House | SettingRow, Select | Autosave | none | defaults from the browser shown as defaults |
| A5 Ready for Pis | T8 | Setup checklist › Ready for Pis | ChecklistStep, StepProgress | none | "Network boot OK · release 1.4.2 ready" | Problem names the failing piece (DHCP, files, release) |
| B1 Pi appears | T8 / T2 | Pis › New Pis waiting | Checklist, EntityList, StatusWordBadge | none | the display shows its code; row "Waiting · seen 21:04" | EmptyState "Plug a Pi into a display and power"; "2 GB, needs 4 GB" |
| B2 Identify | T3 header | Frame page › header | Button, AckBadge | none (action) | "Identifying…" → "Pi confirmed" | "Pi didn't answer" + Retry |
| B3 Name and place | T3 header / T2 | Frame page › header; Frames › wall layout | EntityHeader (inline title), WallMap | Autosave | name saved inline | duplicate name: inline error |
| B4 Fit the picture | T5 | Frame page › Position | LivePreviewEditor, AckBadge, domain corner canvas, nudge pad | Live | Previewing → Presented by the Pi | unbound → binding picker; Pi offline → disabled with since |
| B5 Picture quality | T5 | Frame page › Picture | LivePreviewEditor, SettingRow (actsOn), Slider | Live | AckBadge; each slider "On the display" or "Photo Wall picture adjustment" | display refuses DDC/CI → rows say so, fall back |
| B6 Power | T3 tab | Frame page › Power | SettingRow, Button (Test), AckBadge, LinkToOwner | Autosave | "Display confirmed off" / "Display didn't answer" | no method detected → EmptyState naming smart-plug option |
| B7 First photos | T8 | Setup checklist › First photos | ChecklistStep, MediaGrid | none | step done when a Frame is Showing | Immich not connected → that step first |
| C1 Wall layout | T2 (editable map) | Frames › Wall tab | WallMap (editable), WallTile | Autosave | overlaps flagged | EmptyState "Add your first Frame" |
| C2 Rooms and groups | T2 | Frames › Groups | EntityList, TargetPicker | Autosave | member count | EmptyState: rooms make groups by themselves |
| C3 Copy settings | T7 | Frame page › Picture › Copy to other Frames… | QuickActionSheet, TargetPicker, UndoToast | Autosave (with Undo) | "Copied to 3 Frames" | a display that refuses a value: per-Frame note |
| C4 Two displays per Pi | T3 tab | Frame page › Hardware (Pi page lists both ports read-only) | SettingRow, LinkToOwner | Autosave | "Fed by Pi pw-3f2a · HDMI 2" | port in use: picker shows by which Frame |
| C5 Span | T2 (editable map) | Frames › canvas | WallMap | Autosave | deferred ([roadmap](roadmap.md#deferred-and-what-that-costs)) | none |
| D1 Pick photos | T4 | Scenes › Photo sources › editor | IncludeExcludePicker, MediaGrid, SaveBar | Save | live count and thumbnails | "No photos match" naming the narrowest filter |
| D2 Exclude | T4 | Photo source editor › Exclude | IncludeExcludePicker, SettingRow | Save | count before/after | defaults (archived, hidden, screenshots) shown as defaults |
| D3 Photo fit | T4 section; T3 override | Scene editor › Photo fit; Frame page › Photo fit | SettingsSection, SettingRow (inherited/override), SegmentedControl | Save (Scene); Autosave (Frame) | skipped counts, clickable | "312 too small" links to the list |
| D4 Order | T4 section | Scene editor › Order | SettingRow, SegmentedControl | Save | preview order | none |
| D5 Timing and transitions | T4 section | Scene editor › Timing | SettingRow, Slider | Save | preview plays timing | none |
| D6 Video | T4 section | Scene editor › Video | SettingRow, Slider, Switch | Save | share shown as a count too | no videos match: one-line note |
| D7 Captions | T4 section | Scene editor › Captions | SettingRow, SegmentedControl | Save | preview shows a caption | none |
| E1 Weekly routine | T6 | Schedule › content lanes | TimelineLane, Sheet (block editor), SaveBar | Schedule | now line; block labels | only the Default Scene band + "Add a Schedule" |
| E2 Night off | T6 | Schedule › Power lane | TimelineLane, SettingRow, HoldBanner | Schedule | grey band; "Off 23:00 – 07:00" | a Frame with no power method: lane says "black screen only" |
| E3 Sunrise and sunset | T6 field | Schedule › editor | SettingRow, Field | Schedule | resolved time beside the rule ("sunset −30 min · 18:42") | no House location → link to Settings › House |
| E4 Holidays | T6 | Schedule › editor › Date range | Field, TimelineLane | Schedule | the range overrides blocks visibly | overlaps explained by priority |
| E5 Default content | T6 | Schedule › Default Scene | SettingRow, Select | Autosave | band fills the gaps | none chosen → `todo` chip |
| E6 Why is this playing? | T6 sheet | Schedule block; Frame page › Overview; Home tile | Sheet, LinkToOwner | none | one sentence: "Evenings (Normal, Mon–Fri 17:00–23:00) beats the Default Scene" | Can't tell: "No plan received since 21:04" |
| F1 Glance | T1 | Home › map | WallMap, WallTile, StatusWordBadge | none | live, pushed; "What the Pi is presenting" | per-tile Can't tell; house banner if Central unreachable |
| F2 Skip and previous | T1 tile action | Home › tile; Frame page › header | WallTile actions, AckBadge | none (action) | requested → Pi confirmed | not offered on secured content ([design §12](operator-console-design.md#12-where-a-power-users-expectation-meets-a-written-requirement)) |
| F3 Never show | T1 tile action; T2 | Home › tile; Scenes › Never show list | WallTile actions, UndoToast, MediaGrid | Autosave + Undo | "Won't show again · Undo" | list EmptyState; **Show again** per photo |
| F4 Show now | T7 | Home › Show now; phone tab | QuickActionSheet, TargetPicker, HoldBanner | none (action) + Undo | banner with countdown and **Back to normal** | nothing matches → sheet error inline |
| F5 All off/on | T1 action / T7 | Home › quick actions | Button, HoldBanner, UndoToast | none (action) + Undo | hold shown with who and until | displays that didn't answer listed by name |
| F6 Phone layout | all | AppShell | AppShell, card lists, Sheet | n/a | bottom tabs Home · Show now · Schedule · More | no sideways scrolling anywhere |
| F7 Home Assistant | T9 | Settings › Integrations | SettingRow, Checklist (live checks), Button (Test), ConfirmDangerous (Remove) | Autosave + Test | "Broker connected → Home Assistant online → 12 devices published → last command 21:04" | "Broker rejected the username/password" |
| G1 Health at a glance | T1 | Home › house word; top bar | StatusWordBadge, AppShell | none | six words, rolled up | Can't tell rather than a guess |
| G2 Diagnose | T1 / T3 | Home › problems; Frame page › Overview | ProblemCard, Disclosure (Details), FactRow | none | error template | Details holds the evidence |
| G3 Restart | T3 | Pis › Pi page; Frame page › header (via problem fix) | Button, StepProgress | none (action) | "Reboot requested → started → back online" | step failed with time + Retry |
| G4 Replace a Pi | T7 / T8 | Frame page › Hardware › Replace with…; Pis › New Pis waiting | QuickActionSheet, Checklist, UndoToast | none (action) + Undo | "Living room left is now fed by pw-91c0" | no waiting Pi → EmptyState "Plug in the new Pi" |
| G5 Display changed | T3 + T5 | Frame page › Hardware (prompt) → Position | ProblemCard (`todo`), ChecklistStep, LivePreviewEditor | Live | Frame not ready until Position re-checked | display with no identity: "treated as new" note |
| G6 Updates | T9 | Settings › Updates | SettingRow, StepProgress, ConfirmDangerous | Autosave (choice) + action (Apply) | "1.5.0 downloaded · Apply" then per-Pi progress | download failed: error template |
| G7 Backup and restore | T9 | Settings › Backups | SettingRow, EntityList, ConfirmDangerous (with preview) | Autosave; Restore confirmed | "Last backup 03:00 · 2.1 MB" | no backup yet: EmptyState with "Back up now" |
| G8 Logs | T3 tab | Pis › Pi page › Logs | LogView, SettingRow (log level) | Autosave | following, pushed | Pi offline: last lines kept, Can't tell since |

## 9. Enforcement

Strongest guarantee first. "Enforced" is true on the branch today; "Add" is proposed.

| Rule | Guarantee | Status |
|---|---|---|
| Layers import down; catalogue imports no console model (S1) | test (`tests/test_console_routes_r4.py`) | enforced |
| Pages and domain components do not style (S2) | lint | enforced |
| Colour only from semantic tokens (S3) | lint (+ palette cleared) | enforced |
| Every story renders, passes axe and matches its baseline in dark and light | test (`tests/browser/test_console_catalog_browser.py`) | enforced |
| Catalogue and pages type-check | compile (`npm run typecheck`) | enforced |
| Six words ↔ severity, one mapping (P6) | **compile:** `StatusWord` union and `STATUS_SEVERITY: Record<StatusWord, Severity>` in `design/tokens.ts`; `StatusWordBadge` takes only `StatusWord` | add |
| Done only after the Pi confirms (P3) | **compile/construction:** `LivePreviewEditor` derives Done's state from `Ack` and `latestRevision`; pages cannot pass `doneEnabled` | add |
| Error template has all its parts | **compile:** `ProblemCard` requires `since`, `what`, `doing`, `action` | add |
| Equipment settings say where they act (P2) | **compile:** a `EquipmentSettingRow` variant with `belongsTo` and `actsOn` required | add |
| Every template covers loading, empty, error, Can't tell | **compile:** templates take a `LoadState<T>` discriminated union and render each branch; **test:** each template's stories include `Loading`, `Empty`, `Error`, `CantTell` | add |
| Every page is a template (P7) | **lint:** a page file's default export returns one `patterns/templates/*` component (custom rule or `no-restricted-syntax` on the root JSX) | add |
| Icons only through `ui/icon` | **lint:** `no-restricted-imports` of the icon package outside `src/ui` | add (with the icon choice) |
| Type sizes only from roles | **lint:** clear Tailwind's size scale in `tokens.css` so `no-unknown-classes` refuses `text-sm` outside the roles | add |
| Plain words (P5) | **lint:** a rule over `JSXText` and string props in `src/domain` and `src/pages` refusing the [forbidden words](#forbidden-words), read from one list file (`src/design/words.ts`, which then becomes the list's home and this section links to it) | add |
| Every pattern has stories | **test:** every `src/patterns/*.tsx` has a `.stories.tsx` (a glob check in the catalogue test) | add |
| Phone layout holds (F6) | **test:** template stories also screenshot at a 390 px viewport. Cost: doubles those baselines and their leg time; shard if it passes ~4 min | add |
| Every roadmap step has a translation row | **test:** `scripts/check_docs.py` checks every step id in the roadmap tables appears in [§8](#8-concept--ux-translation) | add |
| One home per setting (P1), one concern per tab, copy tone, one primary per view | **review:** the console-ux skill's checklist | review only |

Costs: the compile-time members constrain props (a page cannot pass free text as a status); the forbidden-words lint will have false positives on proper names and needs an allow list; the phone screenshots cost CI time.
