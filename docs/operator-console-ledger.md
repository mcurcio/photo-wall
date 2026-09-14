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
| 3 | T-inspector (Inspector shell + Binding/Now-showing) | closed | 3650e54 | M1 |
| 4 | Commission-read (read-only Commissioning + gate) | closed | 8572220 | M1 |
| D1 | Docs — M1 (README wall/inspector/now-showing) | closed | 3ecb802 | after M1 (coherence: COHERENT) |
| 5 | B-PATCH (reposition route, LWW) | closed | 047eb00 | M2 |
| 6 | B-DELETE (guarded delete route) [HIGH] | closed | 873d86c | M2 |
| D2 | Docs — M2 (PATCH/DELETE API doc) | closed | e9332b5 | after M2 (coherence: COHERENT) |
| 7 | C-draft (calibration direct-manip + convex guard) | closed | 2cc7437 | M3 |
| 8 | C-lease (preview/commit/revert + lease countdown) [HIGH] | closed | cbe9649 | M3 (adversarial: 1 blocking fixed) |
| D3 | Docs — M3 (Commissioning + lease/conflict) | closed | 63ff1cc | after M3 (coherence: COHERENT) |
| 9 | O-bind (pending rail + bind/unbind + recovery + CTA) | closed | 69eafa6 | M4 |
| D4 | Docs — M4 (onboarding/binding/recovery) | closed | 0e0f48b | after M4 (coherence: COHERENT) |
| 10 | S-place (drag-to-create POST + drag-to-move PATCH) | closed | e355cec | M5 |
| 11 | S-remove (delete + Unplaced tray drag-out) | closed | 708a60c | M5 |
| D5 | Docs — M5 (spatial editing + tray) | closed | e676b0a | after M5 (coherence: COHERENT) |
| 12 | SR-mode (mode toggle + Showrunner shell + R4 + badge) | closed | PEND12 | M6 |
| 13 | SR-sources (Sources list + Refresh) | open | — | M6 |
| 14 | SR-scenes (Scene authoring) [near-ceiling, may pre-split] | open | — | M6 |
| 15 | SR-programs (Programs + N-window helper) | open | — | M6 |
| 16 | SR-runs (Run control + activation outcome + why) | open | — | M6 |
| 17 | X-cutover (flip / to console, retire old page+tests) [HIGH] | open | — | M6 |
| D6 | Docs — M6 (Showrunner + R4) | open | — | after M6 |
| 18 | H-refresh (guidance banner + snapshot clock + cadence) | open | — | M7 |
| D7 | Docs — M7 (refresh model + snapshot age) | open | — | after M7 |
| residual: dedupe-calibration-defaults | share DEFAULT_CORNERS/CROP (useDraft+Commissioning) | open | — | opportunistic (M3 coherence) |
| R-apiwrite | extract shared operator-write helper + frames-API module | closed | 9941612 | M6 start (behavior-preserving) |
