# Operator Console Pass 2, Slice 1: Wall Health at a Glance

**Date:** 2026-09-27 · **Status:** design-gate artifact, awaiting owner approval.
**Builds on:** [the approved console design](operator-console-ux-design.md) (rules R1–R4).
**Layer:** one console increment, plus a small read-only backend addition and one shared constant module. No migration.
**Size:** 6 beads (5 code, 1 docs), about 450 production lines and 520 test lines. Reported failures move to slice 1b (§12).

## 1. The problem in plain words

The operator cannot tell whether anything is broken:

| What the console shows | What it is based on | Why it misleads |
|---|---|---|
| Green "OK" pill | `/healthz` reduced to one word (`useSnapshot.js:233`, `App.jsx:180-186`) | It reports Central only, and the scheduler reason is dropped. |
| Tile dot "Player connected" (`Plan.jsx:47-51`) | `outputs.observation.connected` (`join.js:125-136`) | It is written only at enrollment (`registry.py:135-140`), and a Player enrolls only when its process starts (`player/service.py:517-519`). A Pi that has been powered off for an hour still shows "connected". |
| "Live Display readback: Connected" (`Commissioning.jsx:471-477`) | The same enrollment-time value | It is neither live nor a readback. |
| `players.last_seen` | Written only at enrollment (`registry.py:127-134`) | It is not the last time the Player was heard from. |
| Freshness | Refreshes only on focus or visibility change, or after a write (`useSnapshot.js:116-141`) | Nothing changes while the operator is watching. |
| "Calibration invalid" in alarm orange (`Plan.jsx:379-381`) | `calibration_valid=false` | It shows on every new frame. It is a to-do, not an alarm, and it trains the operator to ignore orange. |
| Inspector | Rendered below the plan (`App.jsx:257-264`); facet reset on every select (`App.jsx:81-84`) | At 1440×900 it is off-screen. |
| Theme and width | `color-scheme: dark` hard-coded (`index.css:5`); no `@media` rules | The page scrolls sideways on phones. |

## 2. The liveness signal

**Liveness means `player_feedback.received_at`, and nothing else.** It is the Central-clock time of the last readiness report Central *accepted*, counted only for the Player's **current** `authority_epoch` and only for a **non-retired** Player.

| Candidate | Written | While the Player is alive | Verdict |
|---|---|---|---|
| `players.last_seen` | At enrollment only (`registry.py:127-134`) | Once per process start | Not liveness. Shown only as "Enrolled N s ago". |
| `outputs.observation.connected` | At enrollment only (`registry.py:135-140`) | Once per process start | A display fact, labelled "at Player start". |
| Open WebSocket session | Nothing is recorded (`app.py:467-522`), and the socket is optional (`service.py:931`) | — | Rejected. |
| **`player_feedback.received_at`** | Upserted on each accepted report (`coordination.py:663-674`) | **Every 0.5 s** (`service.py:859-881`). The sequence number rises on every report (`executor.py:537`), so the not-newer early return (`coordination.py:611-616`) never skips a live Player. | **Chosen.** |

- **Why this is honest.** A rejected report (`unknown_offer`, `stale_binding`, and so on) rolls back and does not count, so "heard" means "Central accepted its report". Enrollment is *not* hearing. A Player that crash-loops through re-enrollment never produces a current-epoch report, so it can never read as healthy (§4).
- **Unbound Players report too.** `advance()` runs every second (`app.py:199-220`). It proposes a plan for every active Player (`coordination.py:402-405`, `planner.py:150,329-335`) and offers even an empty plan (`coordination.py:414-455`). I verified this by reading, and Bead 1 adds a test that pins it.

**The silence threshold has one source: a new stdlib-only module, `contracts/liveness.py`.**

| Name | Value | Current home it replaces |
|---|---|---|
| `REPORT_INTERVAL` | 0.5 s | `service.py:881` |
| `SESSION_BACKOFF` | (1, 5, 15, 60) s | `service.py:70` `BACKOFF` |
| `REQUEST_TIMEOUT` | 15 s | `central_link.py:27` |
| `SILENT_AFTER_SECONDS` | `2*REQUEST_TIMEOUT + SESSION_BACKOFF[0] + REPORT_INTERVAL` = **31.5 s** | new |

**Why that formula.** The threshold tolerates exactly one failed session cycle. A failing request can hang for up to `REQUEST_TIMEOUT` before the cycle ends. The Player then sleeps `SESSION_BACKOFF[0]`. The retry cycle then gets up to one more `REQUEST_TIMEOUT` to deliver its first report, plus one `REPORT_INTERVAL`. A gap longer than that means more than one failed cycle.

The earlier "1+5+15 = 21 s" reasoning ignored the 15 s hang and was wrong. The Player imports all three inputs from this module. Central serves the derived value as `InstallationInventory.silent_after_seconds`, and `health.js` reads it from the snapshot, so no copy of the number exists anywhere else. A pytest pins both the formula and the Player's imports.

**Ages use Central's clock, frozen at read time.** `age = read_at − timestamp`. No browser clock is involved, and nothing ticks between reads. **Labels:** "Last heard N s ago" and "Enrolled N s ago, no report yet". Never "LIVE", "online", or "connected".

## 3. Backend addition (read-only; interface frozen for Bead 1)

| Surface | Owner | Frozen shape |
|---|---|---|
| `Coordinator.player_reports_lock_free() -> PlayerReports` | coordination (owns `player_feedback`) | `PlayerReports` is a frozen model with `read_at: Instant` and `reports: Mapping[Identifier, Instant]` (player id → `received_at`). |
| Details of `player_reports_lock_free()` | | One single-statement read: `players ⋈ player_feedback ON (id, authority_epoch) WHERE retired_at IS NULL`. It runs in a plain transaction and **never takes `COORDINATION_LOCK`** (the docstring says so). `read_at = max(clock.utc(), every timestamp returned)`. |
| `PlayerInventory.last_report_at: Instant \| None = None` | installation_models | Defaulted, like `is_bound` (`installation_models.py:21-24`). |
| `InstallationInventory.read_at: Instant \| None = None` and `silent_after_seconds: float \| None = None` | installation_models | |
| `InstallationInventory.with_liveness(self, reports: PlayerReports) -> InstallationInventory` | installation_models | A pure copy: it sets each Player's `last_report_at`, sets `read_at = max(reports.read_at, every last_seen)`, and sets `silent_after_seconds` from `contracts.liveness`. |
| `GET /v1/operator/inventory` (`app.py:524-530`) | app (composition root) | Returns `registry.inventory().with_liveness(coordinator.player_reports_lock_free())`. The reports are read **after** the inventory. Still admin-only. |

**Invariant: every timestamp in the payload is ≤ `read_at`.** This holds by construction (the reports are read later, and both reads use `max`), so no age can be negative even if the wall clock steps backward.

**Designed twice.**
- **A (chosen): raw facts on the existing inventory, classified in the console.** This follows the `join.js` convention, keeps the one atomic snapshot, and adds no route.
  - *Gives up:* the state precedence lives in JavaScript, so future server-side alerting would have to port it. The threshold does not have this problem; it is already served.
  - *Also:* the two reads are separate transactions, milliseconds apart. A Player that enrolls between them shows "Enrolled, no report yet", which is true.
- **B: a server-derived `GET /v1/operator/wall-health`.**
  - *For:* one reusable source of truth.
  - *Against:* a fourth fetch in the atomic snapshot, a new admin route, and the state machine in Python while the labels and facet routing stay in JavaScript, so it is split in two.
- **Rejected: write `players.last_seen` on every report.** That is a 2 Hz UPDATE on a row locked `FOR SHARE` (`installation_repository.py:40-46`), and it redefines an existing column.

## 4. Per-frame health: one closed set, one classifier

`central/console/src/health.js` is the **only** classifier. It exports:
- `isBound(frame)`: the one definition.
- `playerLiveness(snapshot, playerId)`
- `frameHealth(snapshot, frameId)`
- `facetFor(health, currentFacet)`
- `wallAttention(snapshot)`

`join.js`'s `connectivity()` is deleted, and `boundOutput` stays as a join helper. The Plan tile, Inspector header, Commissioning, Binding, Showrunner, UnplacedTray, EquipmentRail and the strip all consume `health.js`.

States are checked in order and the first match wins. "Age" is always taken from `read_at`, and "limit" means `silent_after_seconds`.

| # | State | Condition | Severity | Label | Facet |
|---|---|---|---|---|---|
| 1 | `unbound` | `!isBound(frame)` | to-do | "Needs a Player" | Binding |
| 2 | `awaiting-report` | Bound, and no current-epoch report | to-do while enrolled age ≤ limit, **alarm** after | "Enrolled N s ago, no report yet" | Binding |
| 3 | `player-silent` | Bound, and report age > limit | alarm | "Player silent · last heard N min ago" | Binding |
| 4 | `display-not-detected` | Bound output's `observation.connected` is false, **or the output row is missing** (fail-closed; the FK at `001_registry.sql:41` makes a missing row unreachable today) | alarm | "No display detected when the Player started" | Commissioning |
| 5 | `needs-commissioning` | `calibration_valid` is false (nothing is shown until committed, `registry.py:369-370`) | to-do | "Needs commissioning" | Commissioning |
| 6 | `ok` | None of the above | ok | "Last heard N s ago" | The current facet (no reset) |

**Precedence rationale.** Rows 1–3 make the facts below them stale. A physical cause comes before a configuration to-do. Only alarms use the alarm colour.

**Design rules.**
1. Only `health.js` classifies.
2. A label states the fact and its age, nothing more.
3. `ok` never implies playback (R2).

## 5. The attention strip

A new `AttentionStrip.jsx`, directly under the status bar.

- **Fixed height.** It is a single line that never reflows the page. Its detail list is a disclosure that **overlays** the content below rather than pushing it down.
- **Summary.** The live region (`role="status"`) carries **state only**, for example "2 frames need attention · 3 to set up" or "All 6 frames heard from". Ages appear as plain text outside the live region, so screen readers are not re-announced every 5 s.
- **List.** Alarms come first, then to-dos, capped at 8 entries followed by "and M more". Each entry reads like "lobby-left — Player silent · last heard 4 min ago".
- **When the scheduler is not ok.** If `/healthz` reports a scheduler status other than `ok` or `disabled`, the strip replaces the N alarm rows with **one** causal line plus a count: "5 frames silent — Central's scheduler is stale; Players cannot report until it recovers."
- **Navigation, Wall mode.** An entry is a button. It sets the Surface to the frame's `surface_id`, selects the frame, opens `facetFor(...)`, **focuses** the Inspector heading, and **scrolls only if the Inspector is off-screen**. Plain tile or tray selection never moves focus.
- **Navigation, Showrunner mode.** Entries are text, not buttons. They do not navigate, so Showrunner work is never abandoned and R4 is preserved.
- **First run.** With no frames, the strip renders nothing and defers to the Guidance banner (`Guidance.jsx:27-30`).
- **Central health is shown in one place: the pill.** It reads "Central: ok" or "Central: scheduler stale" (the reason comes from the `/healthz` body, `app.py:307-343`), or "Central: unreachable". The `aria-label` keeps the "Central health: …" prefix. The strip only ever mentions Central in the causal line above.

## 6. Layout and theming (CSS first)

- **Wide screens (≥ 1024 px):** in Wall mode, `.console__body` becomes a grid, `minmax(0,1fr) minmax(20rem,26rem)`. The Inspector is `position: sticky` with `max-height` and `overflow: auto`, and shows "Select a frame" when nothing is selected (hidden on first run). `max-width` goes from 1180 px (`index.css:20`) to 1440 px.
- **Narrow screens:** one column, with the Inspector after the plan and tray. The only JavaScript is the strip-navigation effect in §5.
- **Colour tokens:** about 30 hex literals collapse into about 12 custom properties, including `--bg`, `--bg-raised`, `--fg`, `--fg-muted`, `--border`, `--accent`, `--ok`, `--todo`, `--alarm` and `--focus`. Dark stays the default. A `@media (prefers-color-scheme: light)` block overrides the tokens, and `color-scheme: light dark` replaces `index.css:5`. SVG fills use the same tokens. I chose this over `light-dark()` for wider support, at the cost of a second value block.
- **At 390 px:** `min-width: 0` on grid and flex children, `flex-wrap` on the header, status bar and token form, and `overflow-wrap: anywhere` on ids (`device-` plus 64 hex characters cannot wrap). The browser test identifies the actual offenders.
- **Dead CSS:** removed in the layout bead (`.plan__badge--*`, `.plan__dot--connected/disconnected/unbound`, `.showrunner__badge--valid/invalid`).

## 7. Polling and the write fence

- **One poller.** `SnapshotProvider` refreshes every 5 s while the tab is visible. On each tick it reads `getToken()` fresh; it never uses a captured token. 5 s matches the calibration overtake poll (`useCalibration.js:16`), whose own timer (`useCalibration.js:204-209`) is deleted. Its detection effect (`:133-200`) still runs on every snapshot.
- **Hidden tabs.** The interval is cleared when the tab is hidden. When it becomes visible again, the poller refreshes immediately and restarts. This replaces the listeners at `useSnapshot.js:116-141`.
- **Newest request wins.** Every refresh (poll, mutation or manual) takes an increasing ticket. A response is applied only if its ticket is newer than the last one applied. The same check guards the **error and 401 branches**, so a stale 401 or 5xx cannot clear the token or overwrite a newer success. Polls are single-flight, because the 15 s fetch timeout (`useSnapshot.js:36`) is longer than the 5 s interval.
- **Write fence.** `apiWrite.js` keeps a write counter, bumped at the **start** of every write, including calibration preview (`useCalibration.js:31`). A refresh records the counter when it starts, and its response is dropped if a write started after that. The refresh `useMutate` issues after a write (`useMutate.js:24-25`) starts after the bump, so it is kept.
- **The draft plane is untouched.** A refresh replaces Plane A only. `useDraft` re-seeds only when `frameId` changes (`useDraft.js:62-73`), and `useCalibration` re-baselines only per frame (`:116-127`). Plan drag state lives in a ref and local state.
- **Stale-snapshot notice.** The status bar reads "updated N s ago — last refresh failed" only after a refresh has actually failed. It is never inferred from age, so there is no flash when returning to the tab.
- **Cost:** three GETs every 5 s per open tab, because the snapshot stays atomic (design §4a).

## 8. What can go wrong

| Failure | What the operator sees | Guarantee |
|---|---|---|
| A Pi loses power | "Player silent" within 31.5 s plus one poll | Test |
| The Player is alive but Central rejects its reports | "Player silent · last heard …". This is honest: the label says heard, not offline | Test |
| A Player crash-loops through re-enrollment | Never `ok`. Each enrollment bumps the epoch, so no current report exists. It shows as `awaiting-report`, but because every loop resets the enrolled age, a loop faster than 31.5 s **stays a to-do and never reaches alarm**. Stated limit; slice 1b | Test (never ok) |
| Offer backpressure: 64 offers in an epoch, then no new offer (`coordination.py:435-437`) | Once the last offer expires, reports fail `unknown_offer` and the frame goes silent. The cause (an execution event) is not surfaced | Test (silent) |
| The scheduler stalls | The pill shows "scheduler stale" within 10 s (`app.py:54`). Frames stay healthy until the last offers expire, **300–330 s later** (`coordination.py:53-54,398-399`). Then they go silent and the strip shows its one causal line | Test |
| Central is unreachable | Frozen states, and the pill reads "unreachable". Silence is never inferred from browser time | Structural |
| A display is unplugged after boot | Not detected until the Player restarts | None. Needs a protocol change (§12) |
| A poll started before a write lands after it | Dropped by the write fence | Test |
| A stale poll returns 401 or 5xx after a newer success | Ignored by the ticket check | Test |
| A drag, draft or calibration preview is in progress during polls | It survives | Structural (R3) and test |
| Console and API versions differ | Not possible: the same app serves `dist` | Deployment |

## 9. Tracer bullet

Beads 1 and 2 run back to back. An accepted report flows through `player_reports_lock_free()`, then `with_liveness`, then `health.js`, and ends as the tile label "Player silent · last heard N s ago". This proves the signal end to end and shows that ages are Central-relative and never negative. **Not covered by the tracer:** the strip, polling, layout, and theme.

## 10. Beads and files (each bead lands green)

| Bead | Files and change |
|---|---|
| **1 Liveness facts** (Python) | **New `contracts/liveness.py`.** **`player/service.py`:** import `SESSION_BACKOFF` and `REPORT_INTERVAL`, dropping the local `BACKOFF` and `.5`. **`player/central_link.py`:** import `REQUEST_TIMEOUT`. **`central/coordination.py`:** add `PlayerReports` and `player_reports_lock_free()`. **`central/installation_models.py`:** add the three fields and `with_liveness`. **`central/app.py`:** compose the inventory route. **`tests/test_registry.py:388-400`:** the equality check now compares against `registry.inventory().with_liveness(...)`. Plus the new pytest cases from §11. |
| **2 Classifier and labels** | **New `health.js`.** **`join.js`:** delete `connectivity`. Route everything through `health.js` in `Plan.jsx`, `Inspector.jsx` (health header), `Commissioning.jsx` (lines 3, 122-123, 471-490: "Display at last Player start", Detected / Not detected), `BindingFacet.jsx:86` (`isBound`, plus "Last heard"), `EquipmentRail.jsx`, `Showrunner.jsx:52-70` and `UnplacedTray.jsx`. **Tests updated in this bead:** `test_operator_wall_browser.py:135,141`; `test_operator_commissioning_browser.py:152-154`, with the test renamed to `…_frame_facts_vs_display_at_player_start` and its `conftest.py:46` CHECKS key renamed with it; `test_operator_showrunner_browser.py:179,182`. |
| **3 Polling and fence** | `useSnapshot.js` (poller, ticket, fence check, `useHealth` returns `{status, reason}`), `apiWrite.js` (write counter), `useCalibration.js` (delete the interval), `App.jsx` (pill text; "last refresh failed"). `test_operator_wall_browser.py:512` still passes because the `aria-label` prefix is kept. |
| **4 Strip and navigation** | New `AttentionStrip.jsx`; `App.jsx` (`selectFrame(id, facet)`, `facetFor`, surface switch); `Inspector.jsx` (focus and conditional scroll on strip navigation only). |
| **5 Layout and theme** | `index.css` (tokens, light block, grid, 390 px fixes, dead-CSS removal), `App.jsx` (column wrappers), `Inspector.jsx` (empty state, hidden on first run). |
| **6 Docs** | `docs/runbook.md` console sections (about lines 350-392): liveness, strip, 5 s poll, "at Player start" wording, and R2's new wording. History line here. |

## 11. Tests and mutation probes

**pytest (Bead 1).**
1. The derivation equals `2*REQUEST_TIMEOUT + SESSION_BACKOFF[0] + REPORT_INTERVAL`, and `player.service` and `player.central_link` use the `contracts.liveness` objects.
2. An accepted report sets `last_report_at`. *Precondition:* advance `ManualClock` between enroll and readiness, so that `received_at ≠ last_seen`.
3. **Unbound-Player service test**, over HTTP: enroll with no binding. Read the inventory **before `advance()`** and see `last_report_at` null. After `advance()`, `/v1/player/state` returns an empty plan, `POST /v1/player/readiness` is accepted, and `last_report_at` is set.
4. Enrolled, no report: `last_report_at` is null and `last_seen` is set.
5. Re-enrollment (epoch bump) hides the old report.
6. A retired Player has no report.
7. A rejected report (`stale_binding`) leaves `last_report_at` unchanged.
8. **Non-negative age:** after the `ManualClock` is set backward, every timestamp is still ≤ `read_at`.
9. **The route answers while `COORDINATION_LOCK` is held** by another connection, well within the statement timeout.
10. `silent_after_seconds` is served.

**Playwright (`tests/browser`).** State is set up with `ManualClock` plus `Coordinator.advance()` and `readiness()`; `page.clock` controls polling.
- *Bead 2:*
  - Silent → "Player silent".
  - A reporting Player → "Last heard".
  - Enrolled with no report → "Enrolled … no report yet", which never reads ok.
  - A never-commissioned frame is "Needs commissioning" with the to-do severity class, not alarm.
  - The Showrunner frame list shows the same labels.
- *Bead 3:*
  - Fast-forwarding 5 s reflects a backend change.
  - While hidden, fast-forwarding 30 s issues no inventory GET.
  - A delayed stale poll (held by `page.route`) is dropped after a newer Refresh.
  - A stale 401 does not log the operator out.
  - A poll that started before a bind is dropped.
  - **A drag across a poll** ends in the dragged placement.
  - **A calibration preview across polls** keeps its draft and countdown.
- *Bead 4:*
  - Counts separate alarms from to-dos.
  - A silent entry opens **Binding** and a needs-commissioning entry opens **Commissioning**, with focus on the Inspector.
  - Showrunner entries are not buttons.
  - Scheduler-not-ok collapses the list to one line.
  - A list over 8 shows "and M more".
- *Bead 5:*
  - At 1440×900 and at 390×844, strip navigation leaves the Inspector `to_be_in_viewport`.
  - At 390 px, `scrollWidth <= innerWidth`.
  - Under `emulate_media(color_scheme="light")`, the body background changes.

**Mutation probes (each must turn the named test red).**

| Break this | Test that fails |
|---|---|
| Drop `authority_epoch` from the join | pytest 5 |
| Source `last_report_at` from `last_seen` | pytest 2 (needs its clock precondition) |
| Take `COORDINATION_LOCK` in the read | pytest 9 |
| Remove the `max` in `read_at` | pytest 8 |
| Hard-code 21 in `health.js` | pytest 1 or the silent browser test |
| Let `awaiting-report` fall through to `ok` | Enrolled browser test |
| Swap rows 3 and 5 | Strip facet test |
| Remove the visibility pause | Hidden test |
| Remove the ticket check on the error branch | Stale-401 test |
| Remove the write fence | Poll-before-bind test |
| Restore the facet reset | Strip facet test |
| Delete the light block | Theme test |
| Add `white-space: nowrap` to ids | 390 px test |

## 12. Costs, deferrals and questions

- **Slice 1b (deferred):**
  - `failure-reported`: the latest report's failures mapped to outputs.
  - Exposing `capacity_ok` and `clock_uncertainty`.
  - Enrollment-loop detection, so that a crash loop reaches alarm.
  - Surfacing the offer-backpressure cause.
- **Deferred beyond 1b:** display hot-plug after boot, which needs a Player→Central message (`OutputReport` is sent at enrollment only), and server-side alerting.
- **Costs:**
  - The state precedence lives in JavaScript.
  - The poll adds three GETs every 5 s per tab.
  - `contracts/liveness.py` couples the Player's retry timing to Central's display threshold. That coupling is deliberate and tested.
- **Question 1:** R2 says "central intent **plus real connectivity**". That connectivity was never real (§1). Should R2 be reworded to "…plus last-heard liveness and display detection at Player start"?
  Build proceeds on the default: reword R2 as proposed (Bead 6).
- **Question 2:** should `failure-reported` (formerly Bead 5, about 60 lines) be in this slice?
  Build proceeds on the default: defer it to slice 1b. It is removed from this slice's beads, tests and state table.

## History

- 2026-09-27: first draft, written after two console audits. The liveness claims were verified against the cited lines.
- 2026-09-27, review round 1: two adversarial reviews failed the draft, and the design was changed in response.
  - Liveness now counts current-epoch reports only, with a new `awaiting-report` state.
  - The threshold moved to `contracts/liveness.py` and was re-derived with the 15 s request timeout (31.5 s, not 21 s).
  - The read interface is frozen, lock-free, with `read_at` as a ceiling on every timestamp.
  - Added the write fence and the error-branch ticket check.
  - The strip is fixed-height, its list is capped, it has a causal line when the scheduler is not ok, and it does not navigate in Showrunner mode.
  - One classifier, and one place that shows Central health.
  - Beads reordered so each lands green.
  - Failures deferred to slice 1b.
