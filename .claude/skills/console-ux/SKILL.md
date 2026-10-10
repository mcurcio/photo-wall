---
name: console-ux
description: Turn one Photo Wall console feature or roadmap step into a normalized UX spec built from the design language. Use it to design, build or spec a console page or feature, to translate a roadmap step (A1…G8) into UI, to add or change a console pattern, or to review console UX in a design or a PR. It is a procedure; the rules it applies live in docs/design-language.md.
---

# console-ux

This skill is a procedure, not a rulebook. Every rule it applies has one home, and the skill links there instead of restating it:

| Rules | Home |
|---|---|
| Principles, foundations, templates, patterns, save models and their assignment, status words, voice, enforcement | [docs/design-language.md](../../../docs/design-language.md) (below: **DL**) |
| What each page holds: object model, navigation, the Frame page, Display, power, Home Assistant, the settings catalogue | [docs/operator-console-design.md](../../../docs/operator-console-design.md) (below: **design**) |
| Step status, delivery order, moments of truth, deferred list | [docs/roadmap.md](../../../docs/roadmap.md) |
| Layers, tokens, severity scale, S1–S3, the look | [decision 0018](../../../docs/decisions/0018-console-by-domain-and-design-system.md) |
| Commands for the console's checks | [AGENTS.md › Verify](../../../AGENTS.md#verify) |

If a rule you need is missing or two homes disagree, stop and report it as a finding; do not write the rule into your spec.

## Procedure

1. **Locate the concept.** Find the step in the [roadmap journey](../../../docs/roadmap.md#the-journey) and its delivery; read the design section that describes it and the step's rows in [DL §8](../../../docs/design-language.md#8-concept--ux-translation). The rows are the starting point; if your design departs from one, the row changes in the same PR.
2. **Name the object and its one home.** Which object from the [object model](../../../docs/operator-console-design.md#2-the-object-model) does this act on, and which page › section is its home ([settings catalogue](../../../docs/operator-console-design.md#11-settings-catalogue) "Lives on")? Everywhere else it appears only as `LinkToOwner` ([DL P1](../../../docs/design-language.md#1-principles)). Use the UI label, never the domain term.
3. **Pick the template** with the [decision table](../../../docs/design-language.md#choosing-a-template): one per home. A live device editor or a quick action is a pattern inside a template, not a page. If nothing fits and the case is not a listed exception, stop and raise it (P7).
4. **Lay out the settings** as [`SettingRow`s](../../../docs/design-language.md#settingrow): its `kind`, the default from the settings catalogue, Reset, inherited or overridden, and for equipment what it belongs to and where it acts.
5. **Take the save model** from the step's DL §8 row; the models are defined in [DL §5](../../../docs/design-language.md#save-models). Note where state is optimistic and where it waits for acknowledgement ([DL §5](../../../docs/design-language.md#optimistic-and-confirmed-state)).
6. **Design every state:** the [template contract](../../../docs/design-language.md#3-page-templates) (loading, empty, error, Can't tell) plus the feature's own states (unbound, Pi offline, not set up, session expired). Feature states are not Error. Every problem uses the [error template](../../../docs/design-language.md#the-error-template) through `ProblemCard`; every Frame status uses the [six words](../../../docs/design-language.md#6-status-and-severity), or `todo` for setup.
7. **Copy pass.** Check every string against the [glossary](../../../docs/design-language.md#glossary), the [forbidden words](../../../docs/design-language.md#forbidden-words), [buttons and verbs](../../../docs/design-language.md#buttons-and-verbs) and [units, times and numbers](../../../docs/design-language.md#units-times-and-numbers).
8. **List the patterns.** Reuse the [catalogue](../../../docs/design-language.md#4-component-and-pattern-catalogue) first. A new pattern goes in `central/console/src/patterns` (a primitive in `src/ui` only if it knows no Photo Wall concept), with a story per state; the catalogue walker renders each story in dark and light. Add it to DL §4 in the same PR. Domain wording stays in `src/domain`; pages and domain components never style (S2).
9. **Write the output:** the UX spec below, its story list and its acceptance checks. Acceptance names the [moment of truth](../../../docs/roadmap.md#the-ten-moments-of-truth) it serves, if any, and splits automated checks (catalogue story, browser journey test) from bench evidence.
10. **Run the automated gates first** on built work: console typecheck and lint, the catalogue test, and `python3 scripts/check_docs.py` (commands in [AGENTS.md › Verify](../../../AGENTS.md#verify)). Fix what they find before the manual review.
11. **Review** with the checklist below: your own spec before handing it on, and anyone's console PR.

## UX spec template

One page per step. Fill every line; write "none" rather than deleting one.

```markdown
## <Step id> <step name> — UX spec

**Object and home:** <UI label> · <page › section> (settings catalogue #…)
**Template:** <T1…T8 or listed exception> (+ pattern inside it) · **Save model:** <name> · **Delivery:** <n>

**Layout** (top to bottom; patterns in `code`):
- …

**Settings:**
| Setting | Control | Default | SettingRow kind | Belongs to | Acts on |
|---|---|---|---|---|---|
| … | … | … | … | … | … |

**States:** Loading … · Empty … · Error … · Can't tell … · feature states …
**Live behaviour:** what is pushed; optimistic vs acknowledged
**Copy:** every visible string, with its glossary term where one applies
**Phone:** what changes below `md`

**Patterns:** reused … · changed … · new … (layer, why no existing one fits)
**Stories:** <Pattern>/<State> …
**Acceptance:** moment of truth … · automated: … · bench: …
**Departs from DL §8 row:** none | <what and why>
```

## Worked example: B4 "Fit the picture"

```markdown
## B4 Fit the picture — UX spec

**Object and home:** Frame · Frame page › Position (settings catalogue #96–#99, #101)
**Template:** T3 Object page + LivePreviewEditor · **Save model:** Live · **Delivery:** 1a

**Layout:**
- `EntityHeader`: "Living room left", Wall and room, status (`HealthBadge`), Identify
- `Tabs` (1a): Overview · **Position** · Picture · Hardware
- `LivePreviewEditor`:
  - controls: `QuadEditor` (the four corners and crop edges over an outline of the output; arrow keys
    move the selected corner), `NudgePad` (1 / 10 / 50 px steps), Rotation, "Reset to full screen"
  - `AckBadge`; **Done** · **Revert**

**Settings:**
| Setting | Control | Default | SettingRow kind | Belongs to | Acts on |
|---|---|---|---|---|---|
| Corner positions | QuadEditor + NudgePad | full output | equipment | frame | pi |
| Rotation | SegmentedControl 0/90/180/270 | 0 | equipment | frame | pi |
| Crop per edge | QuadEditor edges | 0 | equipment | frame | pi |

**States:** Loading: skeleton canvas · Empty: none (a Frame always has a position) ·
Error: Central did not answer (`ProblemCard`, Retry) · Can't tell: last geometry greyed with
since · Feature states: unbound → "Choose which Pi and HDMI port feeds this Frame" with the
picker; Pi offline → editor disabled, "Can't reach Pi pw-3f2a since 21:04"; no acknowledgement
by the deadline → `ProblemCard` inline with Retry and Revert; session expired while away →
"Your unsaved changes were reverted at 21:10 because the editor closed"; leaving with
changes → `LeaveGuard` (Keep or Revert).
**Live behaviour:** every change sends a new revision at once; acknowledged state only;
no session timeout while the tab is open.
**Copy:** "Position", "Previewing — waiting for the Pi", "Presented by the Pi · 21:04:07",
"Done", "Revert", "Reset to full screen". Not "calibration", not "on screen".
**Phone:** mirror above, big arrow buttons and a corner selector below; Done and Revert pinned.

**Patterns:** reused EntityHeader, HealthBadge, SettingRow · changed EntityPage → templates/object-page ·
new Tabs, Row, Stack, SegmentedControl (ui); LivePreviewEditor, QuadEditor, NudgePad, AckBadge,
LeaveGuard (patterns); the Position tab's wiring (domain)
**Stories:** LivePreviewEditor/Clean, /Requested, /Acknowledged, /NoAckProblem, /Unbound,
/PiOffline, /Expired, /Phone; QuadEditor/Default, /CornerSelected, /Cropped; NudgePad/Default;
AckBadge/Requested, /Acknowledged; LeaveGuard/Default; Tabs/Default
**Acceptance:** moment of truth 3 ("Dragging a corner moves the picture on the real display
right away, and Undo works"; in this design its "Undo" is **Revert**, so the check is that
Revert restores the last kept position) · automated: catalogue stories; a browser journey test
that Done stays disabled until the fake Pi acknowledges the latest revision · bench: the
corner moves on the test Pi's portable monitor.
**Departs from DL §8 row:** none
```

The nudge step (catalogue #100) is left out: it is a Position setting of the same home, specified when 1a's beads are cut.

## Review checklist

Each line points to the rule it checks; a "no" is a finding with its file and line. The automated gates of step 10 run first.

- [ ] Each home is one [template](../../../docs/design-language.md#choosing-a-template) or a listed exception, and each tab holds one concern.
- [ ] Every setting is changed only in its home; elsewhere it is a `LinkToOwner` line ([P1](../../../docs/design-language.md#1-principles)).
- [ ] Equipment settings say what they belong to and where they act (P2).
- [ ] Display changes are live, with Done gated on the Pi's acknowledgement and an expiry when the console goes away (P3).
- [ ] Every status has a source and time; nothing claims what the display lights up; no request is shown as done (P4).
- [ ] Frame status is one of the [six words](../../../docs/design-language.md#6-status-and-severity) (or `todo` for setup), drawn by the one chip; accent is never status.
- [ ] Loading, empty, error and Can't tell are designed and have stories; feature states are not Error.
- [ ] Every problem follows the [error template](../../../docs/design-language.md#the-error-template) with one button the system can perform.
- [ ] No [forbidden word](../../../docs/design-language.md#forbidden-words) outside Details, qualified words included; buttons are verb + object.
- [ ] The save model is the one DL §8 assigns; confirm only the irreversible, Undo for the rest.
- [ ] No Refresh button; views update by push ([live updates](../../../docs/design-language.md#live-updates)).
- [ ] The phone layout is designed: no sideways scrolling, 44 px targets ([foundations](../../../docs/design-language.md#2-foundations)).
- [ ] Patterns are reused before new ones; a new one is in DL §4 with its stories; layers and S1–S3 hold.
- [ ] The step's [DL §8 rows](../../../docs/design-language.md#8-concept--ux-translation) match what was built, or change in the same PR.
- [ ] The PR shows before and after screenshots in dark and light ([DL §9](../../../docs/design-language.md#9-enforcement)).
