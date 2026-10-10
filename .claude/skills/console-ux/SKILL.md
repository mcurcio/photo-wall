---
name: console-ux
description: Turn one Photo Wall console feature or roadmap step into a normalized UX spec built from the design language. Use it to design, build or spec a console page or feature, to translate a roadmap step (A1…G8) into UI, to add or change a console pattern, or to review console UX in a design or a PR. It is a procedure; the rules it applies live in docs/design-language.md.
---

# console-ux

This skill is a procedure, not a rulebook. Every rule it applies has one home, and the skill links there instead of restating it:

| Rules | Home |
|---|---|
| Principles, foundations, templates, patterns, save models, status words, voice, the concept → UX table, enforcement | [docs/design-language.md](../../../docs/design-language.md) (below: **DL**) |
| What each page holds: object model, navigation, the Frame page, Display, power, Home Assistant, the settings catalogue | [docs/operator-console-design.md](../../../docs/operator-console-design.md) (below: **design**) |
| Step status, delivery order, deferred list | [docs/roadmap.md](../../../docs/roadmap.md) |
| Layers, tokens, severity scale, S1–S3, the look | [decision 0018](../../../docs/decisions/0018-console-by-domain-and-design-system.md) |

If a rule you need is missing or two homes disagree, stop and report it as a finding; do not write the rule into your spec.

## Procedure

1. **Locate the concept.** Find the step in the [roadmap journey](../../../docs/roadmap.md#the-journey) and its delivery; read the design section that describes it and the step's row in [DL §8](../../../docs/design-language.md#8-concept--ux-translation). The row is the starting point; if your design departs from it, the row changes in the same PR.
2. **Name the object and its one home.** Which object from the [object model](../../../docs/operator-console-design.md#2-the-object-model) does this act on, and which page › section is its home ([settings catalogue](../../../docs/operator-console-design.md#11-settings-catalogue) "Lives on")? Everywhere else it appears only as `LinkToOwner` ([DL P1](../../../docs/design-language.md#1-principles)). Use the UI label, never the domain term.
3. **Pick the template** with the [decision table](../../../docs/design-language.md#choosing-a-template). One template per page; a tab of an object page counts as part of T3. If nothing fits, stop and raise it (P7).
4. **Lay out the settings** as `SettingRow`s ([DL §4](../../../docs/design-language.md#settingrow)) in `SettingsSection`s: for each, the label, the default from the settings catalogue, Reset, inherited or overridden, and for equipment what it belongs to and where it acts (P2).
5. **Pick the save model** from [DL §5](../../../docs/design-language.md#save-models): one per concept, by its "Used for" column. Write down where optimistic and where confirmed state applies ([DL §5](../../../docs/design-language.md#optimistic-and-confirmed-state)).
6. **Design every state** the template requires ([DL §3](../../../docs/design-language.md#3-page-templates)): loading, empty, error, Can't tell, plus the feature's own (unbound, offline, not set up). Every problem uses the [error template](../../../docs/design-language.md#the-error-template) through `ProblemCard`; every status uses the [six words](../../../docs/design-language.md#6-status-and-severity) or the `todo` level for setup.
7. **Copy pass.** Check every string against the [glossary](../../../docs/design-language.md#glossary), the [forbidden words](../../../docs/design-language.md#forbidden-words), [buttons and verbs](../../../docs/design-language.md#buttons-and-verbs) and [units, times and numbers](../../../docs/design-language.md#units-times-and-numbers).
8. **List the patterns.** Reuse the [catalogue](../../../docs/design-language.md#4-component-and-pattern-catalogue) first. A genuinely new pattern goes in `central/console/src/patterns` (a primitive in `src/ui` only if it knows no Photo Wall concept), with a story per state; the catalogue walker renders each story in dark and light. Add it to DL §4 in the same PR. Domain wording stays in `src/domain`; pages and domain components never style (S2).
9. **Write the output**: the UX spec below, its story list and its acceptance checks. Acceptance names the [moment of truth](../../../docs/roadmap.md#the-ten-moments-of-truth) it serves, if any, and which checks are automated (catalogue story, browser journey test) and which need bench evidence.
10. **Review** with the checklist below, on your own spec before handing it on, and on anyone's console PR.

## UX spec template

One page. Fill every line; write "none" rather than deleting one.

```markdown
## <Step id> <step name> — UX spec

**Object and home:** <UI label> · <page › section> (settings catalogue #…)
**Template:** <T1…T9> · **Save model:** <name> · **Delivery:** <roadmap delivery>

**Layout** (top to bottom; patterns in `code`):
- …

**Settings** (label · control · default · belongs to · acts on · inherited?):
| … |

**States:** Loading … · Empty … · Error … · Can't tell … · <feature states> …
**Live behaviour:** what is pushed; optimistic vs confirmed
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

**Object and home:** Frame · Frame page › Position (settings catalogue #96–#101)
**Template:** T5 Live device editor (a tab of T3) · **Save model:** Live, then Done or Revert · **Delivery:** 1a

**Layout:**
- `EntityHeader`: "Living room left", Wall and room, `StatusWordBadge`, Identify, Skip
- `Tabs`: Overview · **Position** · Picture · Power · Photo fit · Hardware
- `LivePreviewEditor`:
  - mirror: a scaled outline of the output with the four corners (domain: `PositionCanvas`)
  - controls: nudge pad (1 / 10 / 50 px, arrow keys), Rotation `SegmentedControl` 0/90/180/270,
    crop per edge (`SettingRow` × 4), "Reset to full screen"
  - `AckBadge`; **Done** · **Revert**

**Settings:**
| Corner positions | canvas + nudge | full output | Frame | the display, through the Pi | no |
| Rotation | SegmentedControl | 0 | Frame | the display, through the Pi | no |
| Crop per edge | Slider (px) | 0 | Frame | the display, through the Pi | no |
| Nudge step | SegmentedControl | 10 px | console preference | n/a | no |

**States:** Loading: skeleton canvas · Empty: unbound → "Choose which Pi and HDMI port feeds
this Frame" with the picker · Error: "Can't reach Pi pw-3f2a since 21:04" (`ProblemCard` inline,
editor disabled) · Can't tell: last geometry greyed · Ack failed: "The Pi didn't confirm"
with Retry and Revert · Leaving with changes: `ConfirmDangerous` Keep or Revert.
**Live behaviour:** every change sends a new revision at once; confirmed state only (the
Pi must present it); no session timeout while the tab is open.
**Copy:** "Position", "Previewing — waiting for the Pi", "Presented by the Pi · 21:04:07",
"Done", "Revert", "Reset to full screen". Not "calibration", not "on screen".
**Phone:** mirror above, big arrow buttons and corner selector below; Done/Revert pinned.

**Patterns:** reused EntityHeader, StatusWordBadge, SettingRow · changed EntityPage (tabs) ·
new Tabs (ui), LivePreviewEditor, AckBadge (patterns); PositionCanvas (domain)
**Stories:** LivePreviewEditor/Clean, /Previewing, /Confirmed, /Failed, /PiOffline, /Phone;
AckBadge/each state; Tabs/Default
**Acceptance:** moment of truth 3 ("Dragging a corner moves the picture on the real display
right away, and Undo works") · automated: catalogue stories; a browser journey test that
Done stays disabled until the fake Pi acknowledges the latest revision · bench: the
corner moves on the test Pi's portable monitor.
**Departs from DL §8 row:** none
```

## Review checklist

Each line points to the rule it checks; a "no" is a finding with its file and line.

- [ ] The page is one [template](../../../docs/design-language.md#choosing-a-template), and each tab holds one concern.
- [ ] Every setting is changed only in its home; elsewhere it is a `LinkToOwner` line ([P1](../../../docs/design-language.md#1-principles)).
- [ ] Equipment settings say what they belong to and where they act (P2).
- [ ] Display changes are live with Done gated on the Pi's confirmation (P3, [save models](../../../docs/design-language.md#save-models)).
- [ ] Every status has a source and time; nothing claims what the display lights up (P4).
- [ ] Status is one of the [six words](../../../docs/design-language.md#6-status-and-severity) (or `todo` for setup), drawn by the one chip; accent is never status.
- [ ] Loading, empty, error and Can't tell are designed and have stories.
- [ ] Every problem follows the [error template](../../../docs/design-language.md#the-error-template) with one button that the system can perform.
- [ ] No [forbidden word](../../../docs/design-language.md#forbidden-words) outside Details; buttons are verb + object.
- [ ] The save model matches [DL §5](../../../docs/design-language.md#save-models); confirm only the irreversible, Undo for the rest.
- [ ] No Refresh button; views update by push ([live updates](../../../docs/design-language.md#live-updates)).
- [ ] The phone layout is designed: no sideways scrolling, 44 px targets ([foundations](../../../docs/design-language.md#2-foundations)).
- [ ] Patterns are reused before new ones; a new one is in DL §4 with its stories; layers and S1–S3 hold.
- [ ] The step's [DL §8 row](../../../docs/design-language.md#8-concept--ux-translation) matches what was built, or changes in the same PR.
- [ ] UI PRs carry before/after screenshots in dark and light.
