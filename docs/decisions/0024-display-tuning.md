# 0024 — Display tuning: the signal range and test patterns

**Date:** 2026-10-13 · **Layer:** system (a third group on the Display's picture, the picture session's test pattern, the Pi's HDMI connector and the Player's final pass) · **Status:** accepted design, built on the branch `feat/display-tuning-tools` (one pull request, after [#95](https://github.com/mcurcio/photo-wall/pull/95)). **Decision owner:** the owner, in chat on 2026-10-13 (his words below). The design's two choices (Q1, Q2) were taken as recommended once he asked for one pull request ("deliver me one PR"); everything not quoted from him is a design choice he may reopen, not his ruling.

**Builds on:** [0022](0022-picture-hardware-and-software-apart.md) (the Display's picture groups, the session, Done, Revert, Copy) and [0023](0023-print-look.md) (the print look and the final pass's order).

## Problem

On the owner's Samsung QN43Q8F the dark areas glow, and the TV hides most of its picture settings. The test Pi (v0.34.0, 4K60, 8 bits per channel) sends HDMI-A-1 with the kernel connector state `broadcast_rgb=Automatic`, `is_limited_range=y`: the kernel picks limited-range RGB (black at 16) for every TV mode, and a TV that expects full range shows that black as grey. The TV's EDID says it follows the range the Pi labels (quantization selectable), so the glow may be the range or the backlight, and nothing in Central could tell them apart: no way to pick the range, and nothing on the screen to compare against. Photo Wall itself lifted no black (black floor 0, a LUT that maps black to 0).

## Requirements (bind)

| # | Requirement | Source |
|---|---|---|
| R1 | "can we set the pi to output full range? Can we make it configurable per display?" | owner, chat, 2026-10-13 |
| R2 | "the easiest way to support that would be to have a button in Central that shows pure white on the frame" (the paper-sheet test: a sheet of white paper taped beside the Frame) | owner, chat, 2026-10-13 |
| R3 | "both. deliver me one PR with all of the tools i need to tune and configure, even when connected to a shitty display with a bright backlight and no controls" | owner, chat, 2026-10-13 |
| R4 | Picture quality includes a grey ramp ("Show grey ramp", not built until now) | [roadmap](../roadmap.md#b-the-first-pi-and-display) B5 |
| R5 | Picture changes are live; Done keeps, Revert or leaving the tab puts back | [console design §5](../operator-console-design.md#5-the-frame-page) Picture, the Live model |
| R6 | Outside every Frame's opening stays black: "outside the configured frame is "someone else's territory"" | owner, 2026-10-10 ([0023](0023-print-look.md) step 8) |
| R7 | Picture settings belong to the Display and move with it | [console design §6](../operator-console-design.md#6-display-identity); [0022](0022-picture-hardware-and-software-apart.md) |

His "both" means R1 and the test pictures. His question "should that be white pre-software adjustments or post-software adjustments?" was answered in chat by the main loop, not by him: post-software, through the Display's Photo Wall (software) chain and the Frame's mat, live while tuning. That answer is a design choice and holds for the look patterns only (below).

## Decision

- **The signal range is the Display's.** `contracts/node_picture.py` `SignalRange`: **Auto** (the kernel's choice, Limited for every TV mode; every Display starts here, which changes nothing), **Full** (black 0, white 255) or **Limited** (black 16, white 235). It is a third group beside 0022's two: stored on the Display (`displays.signal_range`, migration 094), carried by the picture document (`signal_range`), live in the picture session, kept by Done and put back by Revert or leaving the tab (Q1, below), so a range that crushed the shadows never stays. **Copy to other Frames never copies it:** it follows each TV, not a look.
- **A test pattern is the session's, never stored, and bounded on the Pi's clock.** `contracts/tuning.py` `TestPattern`: **White** (`white`, R2's paper test), **Grey ramp** (`grey-ramp`, R4: steps of the signal from 0 to 100 % and a smooth ramp) and **Black level** (`black-level`: raw 0 with raw steps 1 to 20 beside it, after PLUGE, ITU-R BT.814). The session holds at most one ask (`TestPatternAsk`: the pattern and `ask`, the asking edit's sequence; migration 094's `picture_sessions.test_pattern` and `test_pattern_ask`); the picture document carries it only while the session is live; Done, Revert and the session's end drop it. A pattern covers every Frame of the Display's Output, Position's grid included. The Pi takes a pattern off `TEST_PATTERN_SECONDS` (600 s, a placeholder to tune on the bench) after it first received that ask, on its own clock alone (no clock is compared across systems), so a Central outage cannot leave a white screen up; a new ask restarts the bound.
- **Look patterns versus the raw Black level.** White and Grey ramp replace the photos inside each opening and pass through every Photo Wall step and the mat ([0023](0023-print-look.md)'s steps 1 to 7), so they show what photos get. The Black level skips those steps and the mat and sends raw numbers, so no Photo Wall setting can hide or fake a range fault. Outside every opening stays black for all three (R6). Picture operations v6 carry the pattern to the Player (`contracts/node_frame.py` `PictureOperations.test_pattern`); `player/geometry.py` `pattern_signal` is its layout and `final_pixel` the scalar reference of where it enters.
- **The kernel is the truth for the range.** The Pi changes the connector's range only when its value differs from the document's, reads it back and reports it (`SignalReport`: `for_change`, `in_force`, `result` `applied`, `unsupported` or `failed`); applying is idempotent. Auto's outcome is never recomputed: `in_force` is the property's value. While a range changes the wall shows black, never a login prompt or console text.
- **How a range change reaches the screen (Q2: C, set before Weston starts).** Weston (14.0.2, and main) never sends "Broadcast RGB" itself and holds the DRM master, so the range is set while Weston is not running: a privileged pre-start step of `photo-wall-display.service` (`ExecStartPre=`, root) reads the wanted range per connector from a request file the display controller writes and sets it; a path unit watching that file starts a oneshot that restarts the display unit (and so Weston and the Player), resetting the start count of the display unit and of the controller bound to it first, so repeated changes never spend a crash start limit. Weston and the controller stay unprivileged. Details: [display host backend](../display-host-backend.md#units-and-weston-incarnations); the code is `appliance/display_host/signal_range.py`.
- **Done waits for the signal answer.** Done unlocks once the Pi's report answers the latest picture change for every group: the hardware writes, the presented change, and a `signal` whose `for_change` is that change. Any result answers it, as a display write's "did not take it" does; `failed` shows on the tab with the range the TV still gets. A range change pauses the picture and holds that port's DDC/CI and power work until it reports (power first, as 0022).
- **Reading the Black level.** Its one home is the Picture tab's line ([console design §5](../operator-console-design.md#5-the-frame-page) Picture row); the [runbook](../runbook.md#tune-a-display-with-no-controls) links to it.
- **Schema.** The display line's schema major becomes 6; picture operations v6 (the operations text stays within 1024 bytes and no Wayland message changes, so frame protocol stays version 4). Central and the Pis ship together, with no second writer. Migration 094 drops open picture sessions (their drafts lack the range); every Display starts at Auto, so the upgrade alone changes no picture.

### The order in the picture pipeline

| # | Step | Owner | New? |
|---|---|---|---|
| 0 | White or Grey ramp replaces the photos inside each opening | session | new |
| 1–7 | [0023](0023-print-look.md)'s steps (LUT … black floor, mat shadow); the Black level skips them and draws raw numbers | Display, Frame | 0023; the skip is new |
| 8 | Black outside every opening | — | unchanged |
| 9 | Signal range: the connector scales 0–255 into 16–235 when Limited | Display, set by the Pi | new |

### Departures from the design text

The approved design (rev. 2, 2026-10-13) differs from what was built in these points, each a design choice:

- The Black level's wire value is `black-level` (the design wrote `black`).
- The two new session edits (`…/signal-range-edits`, `…/test-pattern-edits`) answer `{sequence}`, like the other picture edits (the design wrote the session view).
- `TestPattern` lives in `contracts/tuning.py`, not `contracts/node_picture.py`: `node_picture` imports `kelvin`, which imports `node_frame`, which needs the pattern too, so the shared vocabulary sits apart from both wires (as a LUT's name lives in `contracts/lut.py`).
- The report names the shown ask as `{pattern, ask}` (`test_pattern_shown: TestPatternAsk | null`; the design wrote the ask number alone).
- An Output with no range property reports `in_force` `auto` (with `unsupported`): it sends what the kernel picks.
- `AppUnitSettling` (the app broker's wait for a unit between states) is declared in `appliance/apps/broker.py`, the port's vocabulary, and `process_linux.py` imports it (the design put it in `process_linux.py`).
- The tab's words for a port with no range setting are "This display's connection has no signal range setting." (the design wrote "This output has no signal range setting."; "output" is not an operator word).
- The connector port is `ConnectorRanges.read(output) -> SignalRange | RangeUnknown` (`NO_PROPERTY` or `UNREADABLE`), with `set` answering whether the read-back equals the range (the design wrote `SignalRangeSetter.current(connector) -> SignalRange | "unsupported"`). Only `NO_PROPERTY` (no such connector, or no range property) is `unsupported`; a connector that did not read (no libdrm, the card node did not open, an ioctl fault) is `failed`, logged in the controller's journal, and asks no restart.

## Alternatives considered

**Q1 · Inside Done and Revert, or saved on change?** Chosen: inside, so Revert or leaving the tab puts back a range that crushed the shadows; each Revert costs one more range change. Rejected: saved on change like Input: one change fewer, but a wrong range stays until someone changes it back.

**Q2 · How a range change reaches the TV.** All but B rely on Weston never sending the setting itself.

| | **C. Set before Weston starts (chosen)** | **A. Screen hand-off** | **B. Teach Weston the setting** |
|---|---|---|---|
| Shape | Weston's start sets the Display's range first; a change restarts Weston and the Player | a privileged one-shot switches the console away, logind pauses Weston, the range is set, Weston resumes | patch Weston's DRM backend to send it; our shell calls it live; offer upstream |
| Proven (probe, test Pi) | the range survived a Weston restart; the restart crashed the app broker (`app_process_ambiguous`, fixed in this pull request; on v0.35.0 it still crashed it, `app_process_incarnation_mismatch`: systemd restarted the Player bound to Weston itself, fixed by one owner of the Player, see Costs) | Weston paused 1.07 s; the switch started a login prompt (`getty@tty2`) on the wall | no |
| Costs | ~10 s without photos per change (measured with the crash; to re-measure); a privileged pre-start step; a path-activated restart that resets the start limit | a spare console kept free of login and text; the TV remote's input dropped per switch; a new way to be stuck on a blank screen | a carried Weston fork rebuilt for each Debian update |
| Gives up | change speed (a TV's range is set about once) | no new failure mode | weeks of packaging; an upstream review we do not control |

Also rejected: our shell using Weston's DRM connection behind its back (the same reliance, plus a mode change racing Weston's own repaint). How long the Samsung itself shows black after a mode change is not measured for any option.

Prior art: Linux "Broadcast RGB" (xrandr, KWin); systemd `ExecStartPre=` and path units; PLUGE (ITU-R BT.814); stepped grey scales.

## Costs

- No setting makes the panel darker than its own black: the Black level shows whether a glow is the range; if it is the backlight, nothing here removes it.
- About 10 s without photos per range change (two for a Full-versus-Limited comparison; measured with the broker's crash, to re-measure on the bench), and once after a Pi start for a Display not on Auto (the kernel starts at Auto).
- The app broker is the Player's one owner (incident 2026-10-13, v0.35.0: `BindsTo=` turned each range change's Weston restart into systemd's own restart of the Player, and the broker exited on a run it never launched until its start limit left it failed, so a Display not on Auto lost its Player at every boot). The Player's unit carries `StopPropagatedFrom=` and `After=` the display unit: a range change stops it, and the broker starts it again for the new Weston at once, never counted against its relaunch pacing. A run the broker did not start, of its own root, is adopted; an observation fault is reported and observed again on a backoff, never an exit. Cost: the Player no longer pulls Weston in; it is started only while Weston is up.
- **A range change interrupts every display of the Pi:** one Weston drives both HDMI ports, so changing one Display's range restarts the picture on the other Display too, and restarts the 10-minute bound of a pattern shown there. The tab says so.
- A privileged step before Weston starts; Weston, the shell and the controller stay unprivileged. It runs before every Weston start, a boot's included, request or not: a Python start with the contracts and libdrm, about 0.13 s on the test Pi 5 (warm cache; a cold boot not measured).
- Each range change restarts the display unit and the controller bound to it; the oneshot resets both units' start counts first, so only crashes count against their start limits (10 in 10 minutes).
- The bound lives in the controller's process: a controller restart (a range change restarts it with Weston) starts the bound again from its own receipt, so a pattern can stay up past 10 minutes by that much.
- **Known limit:** a pattern is drawn by the Player inside each Frame's opening, so an Output with no Frame drawn (unbound, or no Player linked) shows no pattern.
- Deferred: anti-banding noise (dithering) until the Grey ramp shows banding, with its 4K cost measured; a range suggestion from the EDID; colour bars. Not planned: patching Weston (Q2's B).
