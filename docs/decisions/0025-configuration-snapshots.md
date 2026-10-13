# 0025 — Configuration snapshots: the picture and Position history

**Date:** 2026-10-13 · **Layer:** system (Central and the console; no Pi or wire change) · **Status:** accepted design, built on the branch `feat/configuration-snapshots` (one pull request). **Decision owner:** the owner, in chat on 2026-10-13 (his words below). His two answers (scope, use) are current choices he may reopen; everything not quoted from him is a design choice, not his ruling.

**Builds on:** [0022](0022-picture-hardware-and-software-apart.md) (the Display's picture groups, the picture session, Done, Revert, Copy), [0023](0023-print-look.md) (the LUT and the Frame's mat and lights) and [0024](0024-display-tuning.md) (the signal range and test patterns).

## Problem

While tuning a Display against a sheet of white paper ([0024](0024-display-tuning.md)'s paper test), the owner sends phone photos to the agent (an LLM), which reads them. The values on screen when a photo was taken were lost: Central kept only the current picture document per HDMI port (`picture_documents`) and the stored values, and a picture session's draft was dropped at Revert or when the tab closed; the next Done or Copy overwrote the kept values. Position (the Frame fit, the mat and its lights) was the same. Nothing could say what the wall showed at 21:04.

## Requirements (bind)

| # | Requirement | Source |
|---|---|---|
| R1 | "how can we improve the tuning flow so that you can read the values as we go?" | owner, chat, 2026-10-13 |
| R2 | "i like the value of 3 as "configuration snapshots" so it keeps a history of what has been done in the past." | owner, chat, 2026-10-13 |
| R3 | "1 is probably harder to do in a wholly automated fashion because the algorithm would have to deal with noisy pictures. its easier for you (an LLM) to deal with bad picture quality" | owner, chat, 2026-10-13 |
| R4 | Scope: "Everything per Display+Frame (Recommended)" (the option as offered: picture hardware and software, LUT, signal range, test pattern, plus the Frame's mat, lights and position) | owner's answer, 2026-10-13 |
| R5 | Use: "View and restore (Recommended)" (the option as offered: a list with time and values; Restore loads it into the editor, he still presses Done) | owner's answer, 2026-10-13 |
| R6 | One source of truth; no logic in the database (no triggers, no stored procedures); a clock compared only with itself | [design principles](../../CONTRIBUTING.md#design-principles) and the owner's standing rules (design rules, not words of this request) |

"3" in R2 is the third option offered to him (a picture change history); "1" in R3 is the first (matching a photo in the console). The reading that a snapshot also records what is on screen during an open session, marked as not kept, so the agent can read values live by time, is the main loop's, not his words; it was offered as the design's Q3 and taken as recommended.

## Decision

- **Each owner keeps its own history, written where its state converges; a snapshot is composed when read.** A Display's **picture history** (`central/displays/picture_history.py`) and a Frame's **Position history** (`central/fleet/position_history.py`), each a record of what was, never a second source of current state: `displays`, `picture_documents` and `frames` stay the truth. The **configuration snapshot** (R2's word; `central/tuning_history/`) is one Frame at one time: its newest Position entry at or before that time, and the picture entry saying what its Display had then. Nothing stores a snapshot.
- **Kept and sent.** A **kept** entry records a change to what Photo Wall keeps; a **sent** entry records what Central sent the wall, kept or not. Picture: kept entries by Display, written by the one kept write (`write_picture`: Done and Copy); sent entries by HDMI port, written by the picture document's projection each time the port's `picture_change` rises (its number), naming the Display seen and the Frame bound there then. Position: one list per Frame, kept entries from Registry's Save (`commit_calibration_trial_in`) and profile change (`replace_frame_profile`), sent entries from the trial owner each time the values it hands the Node differ from the Frame's newest entry. Revert, a lapsed session, a moved panel and the report judge's fold pass through these points, so no editor writes history.
- **The kinds** (the stored `kind`):

| History | Kind | Means |
|---|---|---|
| Picture, kept | `kept-done` | Done kept a session's draft |
| | `kept-copied` | Copy wrote another Display's values here |
| | `kept-unrecorded` | the stored values had changed outside Photo Wall before this time (rule 3) |
| Picture, sent | `sent-draft` | an open session's draft (and test pattern): not kept |
| | `sent-back` | Revert or a lapse put the wall back from a draft; nothing kept |
| | `sent-kept` | the kept values sent anew (after Done, Copy, a Display newly seen) |
| Position | `kept-saved` | Done (Save) kept a trial's draft |
| | `kept-profile-changed` | the Frame's profile changed; its fit and mat are as they were |
| | `kept-unrecorded` | as for picture |
| | `sent-draft` | a trial's draft handed to the Node: not kept |
| | `sent-back` | the kept values handed back once a draft went |

- **One key, one lock, one clock.** Each entry is numbered per key in commit order under that key's row lock (a Display's kept entries; a port's sent entries take its `picture_change`; a Frame's Position entries) and stamped `at` by PostgreSQL's `clock_timestamp()`, stored as `TIMESTAMPTZ` and answered in UTC. That one clock is compared only with itself: a photo is matched on Central's clock, read as the photo arrives, never by a phone's or a Pi's clock.
- **Rule 3: nothing changes unseen.** A kept write that finds the stored values differ from the key's newest kept entry (a migration, a repair) first records a `kept-unrecorded` entry of what it found; until then the history's reader says `unrecorded_change: true`.
- **Retention (placeholders the owner may tune).** Per key, the newest 500 kept and 20,000 sent entries (about an hour of dragging on Position at about 5 a second); each writer trims its own key under its lock when it appends, so there is no reaper (`central/tuning_history/model.py` `KEPT_ENTRIES`, `SENT_ENTRIES`). A deleted Frame takes its Position history; Displays are never deleted. No entry is written for the past: history starts at the upgrade.
- **Restore loads the editor; Done still keeps (R5).** Each restore rule lives with its owner.
  - Picture (`central/displays/picture.py` `restored`): the entry's hardware codes the display offers now (the rest are skipped and named), its software values with its LUT under today's recipe when the LUT is still on the LUTs list (else the draft keeps its own LUT, and the answer says so), and its signal range. The test pattern is never restored. Only the session's own Display's entries: another Display's values cross only by Copy (`other_display`).
  - Position (`central/fleet/position_history.py` `restore_refusal`, `restored_draft`): refused for another Frame's entry, an entry made for another Frame profile (its corners are fractions of that output) or against another Display than the one seen now; the draft keeps the trial's base revision, so a save made elsewhere is still refused.
- **The read contract** (`central/tuning_history/ports.py` `TuningHistoryQueries`) is one read for the console and the agent alike: a Display's picture history, a Frame's Position history, a Frame's snapshot at a time; plain JSON, newest first. Its routes, tables and SQL are the [runbook's](../runbook.md#configuration-snapshots); the tabs' words are the [console design's](../operator-console-design.md#5-the-frame-page); the agent's procedure is the [display-control skill](../../.claude/skills/display-control/SKILL.md#reading-the-values-while-the-owner-tunes).
- **No database logic.** Every rule is app code; migration 096 adds the three tables and two columns, with no trigger or stored procedure.

## Alternatives considered

**Design it twice.** Chosen (A): per-owner histories at the convergence points, composed when read. Rejected (B, the design's first revision): one per-Frame snapshot written by each editor: one list and a direct read, but two unordered writers on two clocks, blind to every change outside the editors (the report judge's fold, binds, a profile change, migrations), and a Display's picture copied per Frame. A gives up the single list and adds a composer.

**Q3 · Record each change while tuning (sent entries)?** Chosen: yes, grouped by session, so a photo taken minutes ago can still be read. Rejected: kept entries only, with the agent reading the live values as a photo arrives: a later photo could not be read.

**Q1 · Photo matching.** The agent matches a photo against the history by the time on Central's clock as it arrives and by what it shows (R3; Q1 was taken as recommended, not ruled by him). Not planned: an algorithm in the console, which would have to deal with noisy photos, the owner's own concern.

Also rejected: event sourcing (a replay per read) and the existing `audit_events` (an operator action log, not values).

Prior art: SQL:2011 system-versioned tables (a row's history read "as of" a time); Lightroom's Develop History (every step listed, any step loadable, nothing lost until you choose); `picture_change` as the version number.

## Costs

- Sent entries are Central's intent, not the Pi's answer: the Pi presents a change seconds later (about 10 s for a signal range change, [0024](0024-display-tuning.md)), and a test pattern the Pi drops at its own 10-minute bound still shows in the entry until the session ends.
- Changes made with the TV's own remote or menus are never seen (a hardware value changed on the display's buttons shows on the Picture tab, not in the history).
- Five writer sites, and a compare per Node exchange for Position.
- Restore crosses Displays only through Copy; Position refuses another profile's or Display's entry rather than adapting it.
- Deferred: naming and pinning an entry. Not planned: algorithmic photo matching (Q1).

## As built

The interfaces depart from the design text in these points, each recorded in `.claude/errata.md` (configuration snapshots):

- Migration 096, not 095 (095 was taken by an open pull request).
- An entry is named by its UUID (`entry_id`); `restored_from` and both Restore requests name it, not a compound reference.
- History lists are wrapped (`{display | frame, unrecorded_change, entries}`), so rule 3's flag has a field; each Position entry carries `restore_refusal`, Restore's answer for it now, so the tab says why before anyone presses it.
- A picture Restore answers `hardware_skipped` (the codes) and `lut_skipped` (true or false) rather than one list of names, beside `lut_recipe_changed`.
- A Position Restore answers the trial's view (sequence, calibration, mat), since Position's draft is the console's.
- The writers are plain store functions called at the convergence points, inside the same transaction, rather than injected writer objects; `write_picture` takes the record, so no kept write can skip its entry.
- A sent picture entry's kind is decided from the port's previous sent entry: `sent-back` only when a draft went and the values sent differ from it; a Revert of a draft equal to the kept values reads `sent-kept`.
- The history routes live in one read module (`central/tuning_history_routes.py`); the two Restores are their owners' routes.
- Position trims its kept and sent entries apart (500 and 20,000 per Frame), in the one numbering.
- The Picture tab names a history entry's display settings from its own current read (an entry carries codes alone); a code the display no longer lists reads "Setting <code> (display)".
- A Done's kept row usually sits on the tried row of the same values, so it reads "Same values as the change below", its values under Details.
- Restoring a Frame's newest kept Position is no change (the draft equals what is kept), so Done is not offered; a picture Restore makes every restored value the draft, so Done is offered there.
- Restore says "Open while the editor runs" whenever no live session runs.
- Each tab reads its history every 2 s while shown, and again after a Restore, Done or Revert.
