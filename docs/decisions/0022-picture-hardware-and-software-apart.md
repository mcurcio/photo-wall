# 0022 — Picture settings: the display's own and Photo Wall's, each on its own

**Date:** 2026-10-12 · **Layer:** system (what the Picture tab offers, what crosses the wire to a Pi for picture, and how a display's own settings are found) · **Status:** accepted design, being built on the branch `feat/picture-hardware-and-software` (one pull request, after [#86](https://github.com/mcurcio/photo-wall/pull/86)). **Decision owner:** the owner, in chat on 2026-10-12 (his words below). Everything not quoted from him is a design choice he may reopen, not his ruling.

**Supersedes:** roadmap 1c's rule "each setting acts in exactly one place" (brightness and contrast placed on the display or as Photo Wall picture adjustment at the display's first report, and only there), and the part of [0019](0019-first-principles-console.md) decision 4 that reads "brightness and contrast through DDC/CI, Photo Wall picture adjustment otherwise". 0019's "software gain is never called measured panel brightness" stands.

## Problem

Since 1c a Display's brightness and contrast each act in one place: on the display over DDC/CI (the settings channel in the HDMI cable) or as Photo Wall's adjustment of the picture it sends, never both. No other setting the display keeps can be reached from the console. The owner wants both at once, set apart.

## Requirements (bind)

| # | Requirement | Source |
|---|---|---|
| R1 | "when adjusting brightness/contrast/etc i want to independently control the hardware from the software. for example, i i might set hardware brightness to 46% AND the software brightness to 114% if im trying to achieve some specific look" | owner, chat, 2026-10-12 |
| R2 | "everything the hardware supports gets a dedicated configuration (hardware dependent), and then everything the player software supports gets its configuration (standard set)" | owner, chat, 2026-10-12 |
| R3 | Which hardware settings belong on the Picture tab: "power doesnt make sense, but input probably does. volume doesnt make sense in the context of "picture". the "picture" screen should have any hardware settings related to "picture". other screens can have other hardware settings. and if we have a holistic "Display" screen somewhere, then that could have all of them in one place." | owner, chat, 2026-10-12 (answer to the design's Question 1) |
| R4 | Photo Wall's brightness and contrast range: "0–150%" | owner, chat, 2026-10-12 |
| R5 | Copy to other Frames copies "Both (Recommended)" groups; of the display's own settings, "Only ones set in Photo Wall (Recommended)" | owner, chat, 2026-10-12 |
| R6 | Maker-specific codes (E0–FF): "Hide them (Recommended)" | owner, chat, 2026-10-12 |
| R7 | Pis still on the old release after Central deploys: "Yes, accept the gap (Recommended)" (no second writer for the old schema) | owner, chat, 2026-10-12 (answer to the design's Question 2) |
| R8 | Software adjustment is never presented as measured panel brightness; the base page names brightness's source | [requirements](../requirements.md#failure-visibility-and-recovery) U9; [0019](0019-first-principles-console.md) |
| R9 | Picture settings belong to the Display and move with it | [console design §6](../operator-console-design.md#6-display-identity) |
| R10 | Input source: "Let’s put it on both. That seems like the optimal UX" (input control on both the Power tab and the Picture tab; one stored setting, one shared control) | owner, chat, 2026-10-12 (after the design's Question 1) |
| R11 | A display's level in numbers: "Percent, raw count second (Recommended)", e.g. "46 %  (117 of 255)" | owner, chat, 2026-10-12 (picked from offered options) |

## Decision

- **Two groups, never coupled.** Each Display stores **hardware values** (only the display settings someone set in Photo Wall, by their MCCS code, the two-hex-digit number the VESA display-control standard gives each setting: 10 brightness, 12 contrast) and **software values** (always all four: brightness and contrast 0–150 %, 100 % changes nothing; colour temperature; gamma). They are stored, sent, applied, reverted and copied independently; no rule reads one group to decide the other, and neither waits on the other in time: the Pi sends Photo Wall's group to the Player from its own worker, never behind a DDC/CI command; a change writes only the display's codes whose value changed (one that moves only Photo Wall's group runs no DDC/CI command); and the Pi answers a change whose display codes wait for the display or its list, so Done for Photo Wall's group never waits on the display's. The Picture tab shows two sections, **Display (hardware)** and **Photo Wall (software)**: the design's names, which the owner's answer to Question 1 did not rule on.
- **The picture list.** The hardware settings Photo Wall asks a display about are a fixed list, ddcutil's colour group plus sharpness: brightness, contrast, colour preset, colour temperature request, red, green and blue gain, black level and backlight, hue, saturation, six-axis hue and saturation, display mode, sharpness. Not on it: power, input and volume (R3), maker codes E0–FF (R6), position and size on screen (the Position tab owns where the picture sits), menu lock, resets and the display's self-description. The list and each code's name have one home, `contracts/node_picture.py`.
- **Ask, don't trust the self-description.** A display's capabilities string lies (the test monitor lists only code 02 yet answers 10, 12 and D6; ddcutil's docs: "the only way to know for sure … is to try it"), so the Pi asks each code on the list once per display and offers the ones that answer as a level (0 to the display's own maximum) or as a list of choices. A list's choices come from the capabilities string when it names them; a list setting whose choices are not named is shown, not editable.
- **Unset hardware is untouched.** A hardware setting nobody set, or one handed back ("As the display has it"), is absent from what Central sends, and the Pi never writes it (Revert's one-time instruction, below, is the one exception, and it sets nothing).
- **What a display offered is kept.** Its list at the last check is a fact about the display: a report that does not know it (a Pi restart, a pulled cable, a standby that drops hot-plug) never erases it, so the rows keep their ranges and values can be set while the display is away ("Applies when the display is connected"). The tab tells apart not connected, not checked yet and not offered at the last check.
- **The display is read again** (the main loop's addition, 2026-10-12, keeping 1c's re-read; no owner words recorded). Every 60 s, and at once when a display whose list the Pi holds comes back, the Pi reads the held list's values again and reports them; a value set in Photo Wall that the display now holds otherwise says "Changed on the display's buttons" and is not written back until that setting is set again.
- **Picture never costs power.** Picture leaves the Output document and report and gets its own key each way per HDMI port (`picture-<port>`), with its own size cap. On the one DDC/CI port per HDMI connector a power command always goes first; a picture command holds the port for a bounded time (its run is cut there, a write included), and after a power command the port waits a short grace for that operation's next command, so a whole power operation (read, write, read-back) waits at most one picture command. The display line's schema major becomes 4.
- **The Live model stays** (Done waits until the Pi has answered the latest change and the Player presented the software values; Revert puts back the stored values; a touched hardware setting that was "as the display has it" stays so, and the display is put back once to the value the Pi read just before its first write in the session: a one-time instruction beside the stored values in the document until the Pi answers it, never a stored value).
- **Copy** (R5): all four software values; each hardware value set in Photo Wall, where the target display offers that code: a list value only when the target lists the same value and name, a level at the same share of its range (46 of 100 becomes 117 of 255, a design choice). Anything else is left as it is on the target and named as skipped. Never position.
- **Upgrade together** (R7): no second writer for schema 3. Between Central's deploy and a Pi's reboot onto the new release, that Pi follows no new power or picture change.
- **Input on both tabs** (R10, the owner's answer to the open question R3 left): the display's input setting, #117 "Switch the display to this input on power-on", is one stored setting saved through one route (`PUT …/power-settings`) by one row (`InputSettingRow` and its save, `usePowerSettingsSave`, in `central/console/src/domain/frame-power.tsx`), shown on the Power tab and in an Input section below the Picture tab's live editor. It is saved on change, outside the picture session's Done and Revert. #118 ("Never power off while the display shows another input") stays on the Power tab only: it decides a power-off (R3: "power doesnt make sense" on Picture), a design choice.
- **A display's level in numbers** (R11): the Picture tab shows a level as a whole percent of the display's range, rounded half up, with its own count second, "46 % (117 of 255)"; the base page line's "display 46 %" is the same percent. The rule has one home, `contracts.node_picture.HardwareRange.percent`; the console's `levelWords.js` is held to it by a test.

**Data on upgrade** (no backwards compatibility in the data): software brightness and contrast that acted as picture adjustment become twice their old value, capped at 150 % (1c's 50 % meant no change; now 100 % does). A value that acted on the display is forgotten: the display keeps it, but Photo Wall no longer puts it back until it is set again. Open picture sessions are dropped.

## Alternatives considered

| Alternative | Why not |
|---|---|
| **Keep the display's own profile** (ddcutil's `dumpvcp`/`loadvcp`: tune on the display's buttons, "Keep this look" saves the whole colour profile on the Display) | No adjustment from the console, so R1's "hardware 46 %" only by the buttons; Copy would move opaque bytes between models |
| **Every setting the display answers, except power and input** | Offers menu lock, speaker volume and a second way to move the picture beside the Position tab; R3 limits the Picture tab to picture |
| **Keep writing schema 3 beside 4 until no Pi runs the old release** (0017 C12) | A second writer and reader for the display line in Central, deleted in a follow-up; the owner accepted the gap instead (R7) |

Prior art: VESA MCCS and ddcutil (`getvcp`, `capabilities`, the colour feature group); a priority lock (the urgent waiter goes first) for the shared port.

## Costs

- Software brightness above 100 % clipped the brightest parts of a photo to flat white. **Fixed by the highlight rolloff** of [0023](0023-print-look.md): the Display's Soft highlights setting eases values past white into white instead.
- Asking the list holds the DDC/CI port for seconds per new display; power always goes first, but the times are measured on the bench before they are trusted. A picture `setvcp` or `capabilities` slower than the 1.5 s hold is cut (the write is judged by its read-back; list settings stay read-only), and each cut run is logged.
- The re-read holds the port for up to 1.5 s per batch of four codes every 60 s per display, and the report is put again whenever a value changed.
- A value changed on the display's buttons is not put back by a change to other settings, only when that setting is set again or the display is carried out anew. A Revert's instruction a Pi never answers stays pending (it rides each later document) until one does.
- Settings that move each other (a colour preset resetting the gains) are reported, never fought; reverting a preset does not restore the gains it moved. Revert before the Pi's first report in a session leaves a newly set hardware value on the display.
- Setting names are ddcutil's, in English. A list setting with more choices than the wire carries is shown, not editable.
- Old 1c values: picture adjustment above 75 % is capped at 150 %; values that acted on the display are forgotten.
- During the upgrade, each Pi misses power and picture changes until it reboots onto the release (R7).
- Deferred: per-model presets; hardware settings off the picture list (R3: "other screens can have other hardware settings", a holistic Display screen), pending the owner.
