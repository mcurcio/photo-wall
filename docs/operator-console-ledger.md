# Operator Console Redesign — Bead Ledger

One row per bead. Status: open | in_progress | blocked | closed.
Design of record: `operator-console-ux-design.md`. Plan: `operator-console-delivery-plan.md`.
Pass 2, slice 1 (wall health at a glance; design built on its gate defaults, owner approval pending): [operator-console-ux-pass2.md](operator-console-ux-pass2.md).
Pass 2, slice 2 (safe onboarding; design-gate artifact, awaiting owner approval): [operator-console-ux-pass2-onboarding.md](operator-console-ux-pass2-onboarding.md).
Pass 2, slice 3 (Showrunner readability; design-gate artifact, awaiting owner approval): [operator-console-ux-pass2-showrunner.md](operator-console-ux-pass2-showrunner.md).
Pass 2, pass A (stay signed in: session cookie, Log out; design-gate artifact, awaiting owner approval): [operator-console-ux-pass2-session.md](operator-console-ux-pass2-session.md).
Pass 2, passes C+D (familiar look and progressive flows: library-matched tokens, sidebar sections, step flows; built, beads 0, 1a, 1b, 2–5 and D landed; owner approval of questions 1–6 pending, build used their defaults; beads 3–5 integration gate green (pytest 3043, browser 238), review fix cycle landed, final review fix cycle landed (see the ledger row)): [operator-console-ux-pass2-flow.md](operator-console-ux-pass2-flow.md).
Pass 2, pass B (library tag filter, tag suggestions and previews; a Source is one library query shape; reviewed twice (data/caching; security/privacy), revised to revision 5 after a confirmation review; moved to its own PR, https://github.com/mcurcio/photo-wall/pull/37; awaiting owner approval; build on hold): [operator-console-ux-pass2-library.md](operator-console-ux-pass2-library.md) (revision 3 here; revision 5 in mcurcio/photo-wall#37).
Running PR: https://github.com/mcurcio/photo-wall/pull/11 (draft; update its bead table as beads land).
Backend track uses isolated worktrees on Postgres :54332; main/frontend on :54331.
**STATUS: DELIVERY COMPLETE (DONE-GREEN 2026-09-14).** / serves the React console; legacy page retired. Final gate: test_local.py 1122 passed/59 skipped, tests/browser 55 passed. Open residual: dedupe-calibration-defaults (low). PR #11.


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
| 12 | SR-mode (mode toggle + Showrunner shell + R4 + badge) | closed | e8c66a1 | M6 |
| 13 | SR-sources (Sources list + Refresh) | closed | d90e3ce | M6 |
| 14a | SR-scenes-live (scene authoring shell + live-source scene save) | closed | d2b32d6 | M6 (pre-split of 14) |
| 14b | SR-scenes-authored (per-frame candidate choosers + profile hard-filter + save) | closed | 531389b | M6 (pre-split of 14) |
| 15 | SR-programs (Programs + N-window helper) | closed | fcd53bb | M6 |
| 16 | SR-runs (Run control + activation outcome + why) | closed | 962a499 | M6 |
| G1 | SR-retire (player retire control + test) | closed | 380db60 | M6 (parity gap 1; legacy retire target is unbound — parity met) |
| G2 | SR-source-config (create-source form + test) | closed | 2aa6c12 | M6 (parity gap 2) |
| G3 | SR-parity (token-rejection recovery + manual-revert test) | closed | 088e36f | M6 (parity gaps 3,4; ws-fencing arch-specific) |
| 17 | X-cutover (flip / to console, retire old page+tests) [HIGH] | closed | 56163d1 | M6 (adversarial content-parity: PARITY-COMPLETE) |
| D6 | Docs — M6 (Showrunner + R4 + cutover) | closed | 70b9466 | after M6 (coherence: COHERENT; gate 1123/52) |
| 18 | H-refresh (guidance banner + snapshot clock + cadence) | closed | 059ee1c | M7 |
| D7 | Docs — M7 (refresh model + snapshot age) | closed | 4ddbac0 | after M7 |
| residual: dedupe-calibration-defaults | share DEFAULT_CORNERS/CROP (useDraft+Commissioning) | open | — | opportunistic (M3 coherence) |
| R-apiwrite | extract shared operator-write helper + frames-API module | closed | 9941612 | M6 start (behavior-preserving) |
| residual: conftest-evidence | repoint conftest CHECKS at re-hosted console tests | closed | 11f3e35 | operator-browser.json now status=passed; 3 arch-specific items retired |

### Pass 2, slice 1 — wall health at a glance

| Bead | Name | Status | SHA | Notes |
|---|---|---|---|---|
| P2-S1 | Wall health (beads 1–5: liveness facts, classifier and labels, 5 s poll and write fence, attention strip, layout and theme) | in_progress | 0dd6b59, e14b891, b575d68, 4865557, aa45f00 | Beads 1–5 landed; review fix cycle in progress. Errata (a)–(e) applied to the spec. |
| P2-S1-D | Docs — slice 1 (bead 6: runbook wall health, R2 rewording default) | in_progress | — | R2 rewording pending owner confirmation (pass 2 Question 1). |

### Pass 2, slice 2 — safe onboarding

| Bead | Name | Status | SHA | Notes |
|---|---|---|---|---|
| P2-S2 | Safe onboarding (beads 1–6: standing and explicit output chooser, backend rules, ConfirmAction, boot facts, Equipment roster, readable frame ids) | in_progress | 7336afd, 2e5bb19, 25d9501, 0ac87cb, 60d7541, 51f50cb | Beads 1–6 landed. Verifier PASS (pytest 2989 passed, 14 skipped env-gated; browser 108 passed). Review fix cycle 1 in progress. Errata (a)–(f) applied to the spec. |
| P2-S2-D | Docs — slice 2 (bead 7: runbook onboarding, roster, dialogs, retire/replace/revoke, frame ids; retired-device frontier correction) | in_progress | — | Owner questions 1–7 pending; Question 4 (retire the netboot device row) stated truthfully in the runbook. |

### Pass 2, slice 3A — Showrunner readability

| Bead | Name | Status | SHA | Notes |
|---|---|---|---|---|
| P2-S3A | Showrunner readability (beads 3A-1–3A-6: Scene loop, ids and reasons; layout; Central read; rows and precedence; activation; windows and Source form) | in_progress | c9a2505, b918e95, 9c44707, 3e8b29c, 4ea2e31, 1fba0b2; residual test e171caf | Verifier PASS (pytest 2996 passed, 14 skipped env-gated; browser 141 passed; 5 mutation probes red; weekday-evening job walked end to end). Review fix cycle 1 in progress. Errata (a)–(i) applied to the spec. |
| P2-S3A-D | Docs — slice 3A (bead 3A-7: spec errata and fix-cycle decisions, runbook Showrunner, README summary) | in_progress | — | Owner questions 1–6 pending. Also marks the design's J4/§1b and delivery plan's "not on any operator GET" claims superseded, and documents `operator_projection` and `blocking_run_id` in `module-runtime.md`. |

### Pass 2, slice 3B — Scene view and edit; media pipeline and why nothing new

| Bead | Name | Status | SHA | Notes |
|---|---|---|---|---|
| P2-S3B | Scene view and lossless edit (3B-1); media pipeline and "why nothing new?" (3B-2) | in_progress | 27e1cce, 55824af | Verifier PASS (pytest 3002 passed; browser 155 passed; 5 mutation probes red). Errata (a)–(f) applied to the spec. |
| P2-S3B-F1 | Review fix cycle 1: planner `candidate_standing` served as `standing`; Scene revision guard (409 `scene_revision_conflict`, flips Question 4's default, owner to confirm); `candidatesApi.js`; recipe-scoped job counts; wording | in_progress | — (working tree) | Under verification. Residual, not built: serve per-frame `projection.diagnostics` to "Check this frame". |
| P2-S3B-3 | Queue, force and withdraw route (3B-3) | blocked | — | Deferred until the owner answers slice 3 Question 6. |
| P2-S3B-D | Docs — slice 3B (bead 3B-4: spec errata and fix-cycle decisions, runbook Scene edit / media pipeline / why nothing new, `module-runtime.md` guard, `candidate_standing` in `module-planner.md` and `module-authored-media.md`, README) | in_progress | — | Owner questions 1–6 pending; Question 4 now builds on the guard default. |

### Pass 2, pass A — stay signed in

| Bead | Name | Status | SHA | Notes |
|---|---|---|---|---|
| A-1 | Backend: scrypt-keyed total session codec, `admin` dependency (Bearer decides alone, byte compare), sign-in/out routes, no-store middleware and 500 handler | in_progress | a4919db | Verifier PASS (pytest 3059 passed; browser 164 passed; 6 mutation probes red; live curl checks). Security diff review PASS; its P2 (cookie path: now `Path=/v1/operator/`, `__Secure-` over https) fixed in a follow-up (working tree). |
| A-2 | Console: sign-in screen, Log out, marker header on every operator fetch, 403 alert, harness `sign_in` | in_progress | c97dc58 | Same verifier and review; its P3 (a sign-in 204 now leads to Checking before the first refresh) fixed in the same follow-up (working tree). |
| A-3 | Docs — pass A (spec errata and fix-cycle changes, runbook signing in and Log out, README sign-in line, ledger) | in_progress | — (working tree) | Owner questions 1–4 pending; builds on their defaults. |

### Pass 2, passes C+D — familiar look and progressive flows

Gates are the full verify of each bead (pytest, ruff, `check_docs.py`, browser suite); probes are mutation probes, each turning a named test red.

| Bead | Name | Status | SHA | Notes |
|---|---|---|---|---|
| 0 | Task-level browser helpers (`tests/browser/console_tasks.py`) | closed | 3a0b6d8 | Browser 167/167. |
| 1a | Look: tokens for both colour schemes, Console Sans subset and licence, component restyle, `assetsInlineLimit: 0`, `font/woff2` registration | closed | 292c517 | pytest 3024; browser 169; probes red: font rename, input ring, asset inlining, MIME type. |
| 1b | Shell: header, sidebar and drawer, hash routes, three route tables, Needs attention, facet in the route, R4 import scan, sign-in overlay that keeps drafts, `sessionEpoch` on Log out; mode toggle removed | closed | 3e8c7ca | pytest 3033; browser 184; 4 probes red. |
| 1b-review | Review fixes: landing-route race (`ifUnknown`); sign-in dialog above confirmations; hidden pages put their dialogs away; fail-closed R4 scan that also walks the shell; Surface follows the route; memoized pages | closed | eb74dae, 3b14400 | Browser 194, plus the landing-race test (red without the guard). |
| 2 | Scene flow (pilot) with the flow kit; Scene cards; Edit at Review with `baseRevision` and Reload | closed | e6c9862, 14f46a5 | pytest 3034; browser 196; 5 probes red. |
| 2-review | Poll race fix (`aria-busy` while a read is in flight); instance logic lifted into the kit (`useFlowInstance`); activation-retry flake fixed in the harness; review fixes (finish keeps the operator's route, step-scoped reasons, Reload names changes) | closed | fc99a00, 41e5182, 0b5bece, ddc9fcc | Poll race fix: browser 205. Review fixes: pytest 3040; browser 216; 4 probes red. Flake root-caused to Playwright `route(…, times=1)`; fixed test passed 40/40 isolated runs. |
| 3 | Source flow, pass B slot, connection rule, inline Source from the Scene flow (hand-off) | closed | 434d376 | Integration gate for beads 3–5: pytest 3043, browser 238, CI green. Adversarial review FAIL (3 major, 6 minor) → fixed in 3–5-review. |
| 4 | Schedule flow and Program cards | closed | fdf09f6 | Integration gate for beads 3–5: pytest 3043, browser 238, CI green. Adversarial review FAIL (3 major, 6 minor) → fixed in 3–5-review. |
| 5 | Show now flow (`coveringPriority`, activation key in the draft), Run cards, Why disclosures | closed | 564248c | Integration gate for beads 3–5: pytest 3043, browser 238, CI green. Adversarial review FAIL (3 major, 6 minor) → fixed in 3–5-review. |
| 3–5-review | Review fixes: hand-off bound to the draft it was begun for; priority-0 covering Run named; "Another connection…"; protecting Scene refused below a higher Run; shared Scene hand-over with notice (unknown-outcome guard tested); origin refusals keep the Show-now draft; finishing Run not "Past"; restored `-2` absence check; one flow frame and card grid (DRY) | closed | 15b0ba7…a6eefb0 (11 commits) | pytest 3043 (fix cycle's own py gate); browser gate see PR; 5 mutation probes red |
| final-review | Final review fixes (PR #34): a flow's write holds its draft and binds its answer to the draft's id (`useFlowWrite`, the four flows' write boilerplate folded into the kit); an unanswered save "may have been saved" and is not a collision with itself; R4's graph from the bundler, cross-checked with the scan, forbidding the Wall-only closure and the calibration route; light `--warn`/`--alarm` darkened for text on tints, with composite contrast pairs; 390 px checks on every Show page; Show now says a protecting Run refuses it at any priority; Scene hand-over as an event; one Back after Save leaves the section; route ids never echoed; Schedule seed skips a deleted Scene; test and doc hygiene | closed | 24d13f5…(this cycle's docs commit) | Gate at 03bce9b plus these docs: pytest 3050 passed (316 skipped env-gated, 1 xfailed), ruff, lint-imports, `check_docs.py`; browser 255 passed. Probes red: the draft-id binding removed (late answer after Log out), reseed while busy (card Show now during an activation in flight), the attempted-id exclusion dropped (Scene and Program unanswered saves), the bundler cross-check removed (the four JSX-text self-tests), the contrast fix reverted (light contrast pairs), and a Scene/Schedule overflow the old Sources-only check passed. |
| D | Docs: runbook by job, README, `architecture.md` console section, pass A follow-up (session §7, Question 4), flow design status and history, ledger | closed | 1bc0db5 + follow-up | `check_docs.py` passed. Owner questions 1–6 pending; build used their defaults. |
