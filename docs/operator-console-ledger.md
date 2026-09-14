# Operator Console Redesign — Bead Ledger

One row per bead. Status: open | in_progress | blocked | closed.
Design of record: `operator-console-ux-design.md`. Plan: `operator-console-delivery-plan.md`.
Running PR: https://github.com/mcurcio/photo-wall/pull/11 (draft; update its bead table as beads land).
Backend track uses isolated worktrees on Postgres :54332; main/frontend on :54331.

| Bead | Name | Status | SHA | Notes |
|---|---|---|---|---|
| 0 | F-shell (React toolchain + /console shell) | closed | 07a27fd | M1 |
| 1 | T-plan (per-Surface SVG plan + Unplaced tray) | closed | acbb70d | M1 |
| 2 | T-join (now-showing join + tile chips) | closed | 226d855 | M1 |
| 3 | T-inspector (Inspector shell + Binding/Now-showing) | closed | 3d9530e | M1 |
| 4 | Commission-read (read-only Commissioning + gate) | in_progress | — | M1 |
| D1 | Docs — M1 (README wall/inspector/now-showing) | open | — | after M1 |
| 5 | B-PATCH (reposition route, LWW) | closed | 047eb00 | M2 |
| 6 | B-DELETE (guarded delete route) [HIGH] | open | — | M2 |
| D2 | Docs — M2 (PATCH/DELETE API doc) | open | — | after M2 |
| 7 | C-draft (calibration direct-manip + convex guard) | open | — | M3 |
| 8 | C-lease (preview/commit/revert + lease countdown) [HIGH] | open | — | M3 |
| D3 | Docs — M3 (Commissioning + lease/conflict) | open | — | after M3 |
| 9 | O-bind (pending rail + bind/unbind + recovery + CTA) | open | — | M4 |
| D4 | Docs — M4 (onboarding/binding/recovery) | open | — | after M4 |
| 10 | S-place (drag-to-create POST + drag-to-move PATCH) | open | — | M5 |
| 11 | S-remove (delete + Unplaced tray drag-out) | open | — | M5 |
| D5 | Docs — M5 (spatial editing + tray) | open | — | after M5 |
| 12 | SR-mode (mode toggle + Showrunner shell + R4 + badge) | open | — | M6 |
| 13 | SR-sources (Sources list + Refresh) | open | — | M6 |
| 14 | SR-scenes (Scene authoring) [near-ceiling, may pre-split] | open | — | M6 |
| 15 | SR-programs (Programs + N-window helper) | open | — | M6 |
| 16 | SR-runs (Run control + activation outcome + why) | open | — | M6 |
| 17 | X-cutover (flip / to console, retire old page+tests) [HIGH] | open | — | M6 |
| D6 | Docs — M6 (Showrunner + R4) | open | — | after M6 |
| 18 | H-refresh (guidance banner + snapshot clock + cadence) | open | — | M7 |
| D7 | Docs — M7 (refresh model + snapshot age) | open | — | after M7 |
