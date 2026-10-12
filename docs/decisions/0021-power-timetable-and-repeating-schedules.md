# 0021 — The power timetable and repeating Schedules

**Date:** 2026-10-12 · **Layer:** system (who decides display power and what plays, and what crosses the wire to a Pi) · **Status:** built (pull request [#86](https://github.com/mcurcio/photo-wall/pull/86), roadmap deliveries [2a](../roadmap.md#2a-power-night-off-and-all-offon) and [2b](../roadmap.md#2b-the-week), one pull request: owner, 2026-10-11: "launch a design for 2a and 2b as one deliverable unit"). **Decision owner:** the owner, at two gates on 2026-10-12: the [overview gate](https://claude.ai/artifact/8DXMFAosB3HKY7m35fmHno) and the [system gate](https://claude.ai/artifact/HHNRy2Hs1CpEm5VVK58DWm). Everything below that is not quoted from those gates is a design choice he may reopen, not his ruling.

## Problem

Before 2a and 2b the Schedule editor wrote N separate Programs, each one fixed window timed in the browser's timezone, which Central never recorded; the house had no timezone or location; nothing turned displays off at night or held them; and no Frame could say why it showed what it showed. Night off must keep working while Central is away, which means the Pi must act at a time of day, while the wire rule allowed only durations to cross ("durations, never timestamps", as the design frame put the rule that a clock is compared only with itself).

## Requirements (bind)

| Requirement | Source |
|---|---|
| Night off: displays actually off, by default 23:00–07:00, and back on; All off / All on, holding until the next scheduled power change | [roadmap 2a](../roadmap.md#2a-power-night-off-and-all-offon) (E2, F5) |
| "I meant power as a unique concept, but it could go either way. Or both." | owner, 2026-10-09, [requirements › operator experience](../requirements.md#operator-experience) |
| Display power is operational state, kept apart from authored content | [requirements › operations](../requirements.md#operations-and-scope) |
| "Help, not permission"; "Last good wins, until its own data runs out … Central going quiet is never by itself a reason to stop"; "Clocks are never compared across systems" | [0016](0016-central-and-node-relationship.md), owner-written, 2026-10-05 |
| One repeating Schedule (weekly, across midnight and daylight saving); sunrise/sunset; a Default Scene for the gaps; every Frame's Why names the winner and what it beat | [roadmap 2b](../roadmap.md#2b-the-week) (E1, E3, E5, E6) |
| The Installation (the console's House) owns policy and timezone | [requirements › installation model](../requirements.md#installation-model) |
| Authored edits apply from the next Run | [requirements › Run behavior](../requirements.md#run-behavior) |
| "Yes, send 7 days of set times" (may Central send each Pi the next 7 days of off and on times) | owner, 2026-10-12, [overview gate](https://claude.ai/artifact/8DXMFAosB3HKY7m35fmHno) |
| "Amend U1" (intended darkness, authored or operational, is allowed and reads Resting; the amended text is [U1](../requirements.md#failure-visibility-and-recovery)) | owner, 2026-10-12, [system gate](https://claude.ai/artifact/HHNRy2Hs1CpEm5VVK58DWm) Question 1 |
| "Not now" (may a Scene ask for its displays on or off, #70 with its Night off guard #89) | owner, 2026-10-12, same page, Question 2 |
| No backwards compatibility unless the owner asks | owner's standing rule (global instructions) |

## Decision

Design it twice: the frame weighed where the weekly power rule is evaluated (A, B′, and two smaller options below) and chose **B′: Central decides and sends a 7-day timetable of set times; the Pi switches on its own clock.**

- **The wire rule** becomes: **durations and calendar set times cross the wire; never a clock reading.** A set time is a calendar time Central worked out from the house's rules; the Pi compares it only with its own clock; a test's duration counts on the Pi's monotonic clock. This wording is the design's; its source is the owner's answer "Yes, send 7 days of set times" (2026-10-12). Its code home is the module docstring of [`contracts/node_output.py`](../../contracts/node_output.py).
- **One decider per question** (a design rule). Central decides what plays (Runtime) and whether each display should be on (the power timetable, `central/power/timetable.py`). The Pi decides only when its own clock reaches a set time, and reports what it did (`appliance/display_host/power_timetable.py`, `output_power.py`).
- **The Output document's power part** per HDMI port: `now` (Central's decision, in force from when the Pi reads it), `changes` (the set times, strictly increasing), `until` (the horizon's end), `darkness` (display off or black picture), and 1b's `test` on top. The display line's schema major is 3. The horizon runs to the start of the eighth house day after today, so it moves once a day, at house midnight, and always covers at least 7 days; the worker rewrites the document at each set time, horizon move and hold end, and at every power or house write.
- **House time** (`central/house_time/`): the house's zone (never empty: seeded at Central's start from its own zone) and optional location, and the one calculator that turns a When (days, a start and an end, each a clock time or sunrise/sunset ± minutes) into moments. A skipped daylight-saving time moves forward by the gap; a repeated one takes its first occurrence (iCalendar's rule); a day with no sunrise or sunset has no window. Sun times are `astral`'s, in Central only: the Pi has no zone database and no sun maths.
- **Two arbiters, one When** (a design rule). Content: priority (Default Scene < Normal < Special event < Always wins), a tie to the request admitted last, as Runtime did before. Power: the newest hold until the next scheduled change (or until resumed, #90), Night off underneath, on otherwise; the 1b test on top of both. Neither arbiter reads the other.
- **Programs repeat** (the console's Schedules): a Program is a Scene, a When and a priority; its windows are worked out in house time and never stored; a window's identity is `program:<id>:<revision>:<start>`. The Default Scene is one perpetual Run at the lowest rank. Save applies from the next window; Save and apply now finishes the playing window gracefully and plays the new rule at once ([Runtime](../module-runtime.md#house-time-and-repeating-programs)).
- **Why explains, never decides** (`central/why/`): each Frame's winner and what it beat, its wanted power with the reason and the next set time, and what the Pi says (a fact that may lag).
- **Today's Programs end at deploy** (migration 089): they were single windows timed in the browser's zone, which Central never recorded, so they cannot be converted faithfully. No backwards compatibility.

**Departures from the frame**, recorded here:

- `OutputPower` gains `until`, the horizon's end, so "the timetable ran out" has a set time of its own; the frame's interface had only `now` and `changes`, and the report's `timetable-ran-out` needed a moment to be judged against.
- The Pi acts on its clock whether or not that clock was synchronised at boot. The frame's failure table already accepted that a wrong Pi clock switches at the wrong time; no "clock trusted" gate stands between the Pi and Night off (0016: no check that can fail stands between a Node and its work).
- Scene power requests (#70, #89) are not built (owner, 2026-10-12: "Not now"); the timetable can take them later as another layer.

## Alternatives considered

| Alternative | Why not |
|---|---|
| **A: the Pi keeps the weekly rule itself** | Bends the wire rule further, and each Pi needs a timezone database and sunrise maths that must match Central's exactly; two evaluators of one rule disagree at the edges (daylight saving, polar days) |
| **Durations only** ("off in 2 h 10 min, on 8 h later"), counted from when the Pi reads them | Switch times slip by however late a document arrives, and every re-sent document restarts the counts |
| **Central flips documents at the moment of change** (Central sends "off now" at 23:00) | Night off stops while Central is away, against 0016's "Central going quiet is never by itself a reason to stop" |
| Keep single-window Programs and convert today's copies into one rule | Their times were in the browser's zone, which Central never recorded, so any conversion guesses |

Prior art: iCalendar (RFC 5545) weekly recurrence and its daylight-saving rule for a When; a thermostat's hold ("until the next scheduled change") for holds; Gray and Cheriton leases (each side times a duration on its own clock) for the test.

## Costs

- **A 7-day horizon.** After 7 days without Central the displays stay as they are (reported `timetable-ran-out`). A Pi that boots without Central has no document and no Night off, as before.
- **Daily rewrites.** Every Output document is rewritten at least once a house day as the horizon moves; a rewrite that wants the same as the Pi is doing writes nothing to the display.
- **Central and the Pis upgrade together.** The display line's schema 3 is not read by an older Pi, which refuses the document and leaves its display as it is until it reboots onto the release.
- **A wrong Pi clock switches at the wrong time.** The Pi's wall clock is trusted as it is.
- **Today's Programs end at deploy.** The owner picks a Default Scene and remakes his routine; until then Frames keep their last photo.
- **Runtime forgets after a day.** Every repeating window and Default Scene Run adds a Run and an admission; Runtime forgets each once it and its window ended more than 24 hours ago, so its stored state stays bounded. An operator retry of a request whose Run ended over a day ago is a new request.
- **A house zone change waits for the next window.** A window it moves into the past is not played backdated.
- **Weekly only.** No one-off dates, holidays or "every other week" (E4, deferred).
- **Larger than planned.** L in one pull request, not M + M.
