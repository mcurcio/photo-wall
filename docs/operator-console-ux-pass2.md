# Operator Console Pass 2, Slice 1: Wall Health at a Glance

**Date:** 2026-09-27 · **Status:** design-gate artifact, awaiting owner approval.
**Builds on:** [the approved console design](operator-console-ux-design.md) (rules R1–R4).
**Layer:** one increment to the console plus one small, read-only backend addition. No migration.
**Size:** 6 beads (5 code, 1 docs), about 400 production lines and 380 test lines. Bead 5 can be cut.

## 1. The problem in plain words

The operator cannot tell whether anything is broken. What the console shows today:

| What the console shows | What it is based on | Why it misleads |
|---|---|---|
| Green "OK" header pill | `/healthz`, reduced to one word (`useSnapshot.js:233`, `App.jsx:180-186`) | It reports Central only. The scheduler reason is dropped. |
| Tile dot "Player connected" (`Plan.jsx:47-51`) | `outputs.observation.connected` (`join.js:125-136`) | This value is written only at enrollment (`registry.py:135-140`), and the Player enrolls only when its process starts (`player/service.py:517-519`). A Pi that lost power an hour ago still shows as connected. |
| "Live Display readback: Connected" (`Commissioning.jsx:471-477`) | The same enrollment-time value | This is not a readback, and it is not live. |
| `players.last_seen` | Written only at enrollment (`registry.py:127-134`) | This is enrollment time, not the last time Central heard from the Player. |
| Data freshness | Inventory is refreshed only on focus or visibility change, or after a mutation (`useSnapshot.js:116-141`) | Nothing changes while the operator is watching. |
| "Calibration invalid" in alarm orange (`Plan.jsx:379-381`) | `calibration_valid=false` | Every new frame shows this. It is a to-do, not an alarm, so the operator learns to ignore orange. |
| Inspector position | Rendered below the plan (`App.jsx:257-264`), and the facet resets to Commissioning (`App.jsx:81-84`) | At 1440×900 it is off-screen. |
| Theme and narrow screens | `color-scheme: dark` is hard-coded (`index.css:5`) and there are no `@media` rules | The page scrolls sideways on phones. |

## 2. The liveness signal (Q1)

**Answer:** use `player_feedback.received_at`. This is the Central clock time of the last readiness report Central accepted from the Player's current session.

| Candidate | Written where | How often while the Player is alive | Verdict |
|---|---|---|---|
| `players.last_seen` | `registry.py:127-134`, at enrollment only | Once per Player process start | Rejected. Useful only as a floor ("heard at enrollment"). |
| `outputs.observation.connected` | `registry.py:135-140`, at enrollment only | Once per process start | Rejected as liveness. Kept as a display fact labelled "at Player start". |
| Open WebSocket session | `app.py:467-522` keeps no registry of sessions, and the WebSocket is optional (`service.py:931`) | None | Rejected. Nothing is recorded. |
| `execution_events` failures (`coordination.py:317-341`) | Written only when something fails | Irregular | Rejected as liveness. The latest report's failures are used in Bead 5. |
| **`player_feedback.received_at`** | `coordination.py:663-674`, upserted on every accepted readiness report | **Every 0.5 s** (`service.py:859-881`). The sequence number increases on every report (`executor.py:537`), so the "not newer" early return (`coordination.py:611-616`) never skips a live Player. | **Chosen.** |

**Unbound Players report too.** The scheduler runs `advance()` every second (`app.py:199-220`). `advance()` builds a proposal for every active Player, including Players with no bindings (`coordination.py:402-405`, `planner.py:150`, `planner.py:329-335`), and offers each one a plan (`coordination.py:414-455`). An empty plan is allowed (`contracts/models.py` `Plan.layers`), so a pending Pi reports every 0.5 s. I verified this by reading the code, not by running it. Bead 1 adds a test that pins it.

**What the signal means.** "Last heard" is the later of `players.last_seen` (enrollment) and `received_at` for the Player's current `authority_epoch`. A row from an older epoch is ignored; cleanup deletes it anyway (`coordination.py:506-514`). A readiness report that Central rejects (`unknown_offer`, `stale_binding`, and so on) rolls back and does not count. "Heard" therefore means "Central accepted its report", which is the honest meaning.

**Staleness threshold: 30 s, derived from the Player's own timing.** A healthy Player reports every 0.5 s. After a failed session cycle it retries after 1 s, then 5 s, then 15 s, then 60 s (`BACKOFF`, `service.py:70`, `service.py:960-962`). A gap longer than 1 + 5 + 15 = 21 s therefore means at least three consecutive failed cycles. 30 s rounds that up and matches the Player's own rule that a session lasting 30 s counts as healthy (`service.py:949`). The console holds this as a named constant, with this derivation in a comment. The coordinator's 2 s `readiness_seconds` (`coordination.py:56`, `762-763`) is a commit gate and too tight for display; it would flap.

**How ages are computed.** Ages are Central-relative and frozen at read time: `age = read_at − last_heard`, where both values come from Central's clock. There is no browser clock skew and no ticking between reads. How recent the page is overall is already shown by the "updated N s ago" bar.

**Labels.** Only "Last heard 12 s ago" and "No problems reported". Never "LIVE", never "online", never "connected".

## 3. The backend addition (read-only, no migration)

| Change | Owner | Shape |
|---|---|---|
| New read `Coordinator.player_reports()` | coordination (it owns `player_feedback` and `plan_offers`) | Returns a map from Player id to its report: `received_at` and `failures`. Only current-epoch, unretired Players are included. It runs in a plain read transaction and does **not** take `COORDINATION_LOCK`, so it never waits behind `advance()`. |
| `PlayerInventory.last_report_at: Instant \| None = None` | installation_models | Defaulted, like `is_bound` (`installation_models.py:21-24`). |
| `PlayerInventory.reported_failures: tuple[ReportedFailure, ...] = ()` (Bead 5) | installation_models | `ReportedFailure = {output_id: Identifier \| None, code}`. `code` is already constrained to `^[a-z_]{1,64}$` (`contracts/models.py:224-226`). |
| `InstallationInventory.read_at: Instant \| None = None` | registry sets it from its clock inside `inventory()` | This is the reference point for every age. |
| `GET /v1/operator/inventory` (`app.py:524-530`) | app, the composition root | Returns the registry inventory merged with `player_reports()` through a pure helper next to the model. Still admin-only. |

**Failure mapping (Bead 5).** A failure's `assignment_id` is resolved to an `output_id` through the offer the report named (`plan_offers` row `(player, epoch, report.revision)`). Readiness is only accepted against a live offer (`coordination.py:604-610`), and cleanup deletes only expired offers (`coordination.py:484-500`), so a fresh report's offer is present. A failure that cannot be mapped is attached with `output_id: null` and flags every frame of that Player. This over-reports rather than hiding the failure.

**Designed twice.**
- **A (chosen): raw facts on the existing inventory, health derived in the console.** This follows the existing `join.js` convention, needs no new route, and keeps the snapshot to one atomic fetch.
  - *Gives up:* the 30 s threshold and the state precedence live in JavaScript. A future alerting feature would have to move them server-side.
  - *Also:* the inventory and the reports are read in two transactions, milliseconds apart. A Player that enrolls in between shows no report yet and falls back to "heard at enrollment", which is harmless.
- **B: a server-derived `GET /v1/operator/wall-health` returning per-frame states.**
  - *For:* one server-side source of truth that other clients could reuse.
  - *Against:* a fourth fetch inside the atomic snapshot, a new admin route, and a state machine in Python while the labels and facet routing stay in JavaScript, so it would be split in two.
- **Rejected: write `players.last_seen` on every readiness report.** That is a 2 Hz UPDATE on a row other transactions lock `FOR SHARE` (`installation_repository.py:40-46`), and it changes what an existing column means.

## 4. Per-frame health: one closed set, one pure function

`frameHealth(snapshot, frameId)` lives in a new pure module, `central/console/src/health.js`. It sits beside `join.js` (joins) and `projection.js` (geometry), and it reads bindings only through `join.js`'s `boundOutput`. The tile, the summary strip, the Inspector header, the Showrunner frame list, and facet routing all consume this one function. Nothing else maps states to labels, colours, or facets.

States are checked in order and the first match wins. The order is the precedence.

| # | State | Condition | Severity | Label (tile, strip, Inspector) | Facet opened |
|---|---|---|---|---|---|
| 1 | `unbound` | `frame.player_id` is null | to-do | "Needs a Player" | Binding |
| 2 | `player-silent` | Bound, and the time since Central last heard from the Player is over 30 s | alarm | "Player silent · last heard 4 min ago" | Binding (swap or retire lives here) |
| 3 | `display-not-detected` | The bound output's `observation.connected` is false | alarm | "No display detected when the Player started" | Commissioning |
| 4 | `failure-reported` (Bead 5) | The Player's latest accepted report lists a failure for this output, or an unmapped failure | alarm | "Player reports a problem: capacity" | Now-showing |
| 5 | `needs-commissioning` | `calibration_valid` is false (the Player shows nothing until it is committed, `registry.py:369-370`) | to-do | "Needs commissioning" | Commissioning |
| 6 | `ok` | None of the above | ok | "No problems reported · last heard 2 s ago" | The operator's current facet (no reset) |

**Why this order.** Rows 1–2 make the facts below them stale or meaningless, so they come first. Physical causes (row 3) come before symptoms (row 4). Alarms come before to-dos. I named row 3 `display-not-detected` rather than `display-disconnected` because the fact dates from the Player's process start; it is not a current reading.

**Three design rules.**
1. Only `health.js` classifies.
2. Only alarms use the alarm colour. To-dos use a neutral "to-do" token.
3. A label says what the fact is and how old it is, never more than that.

`facetFor(state, currentFacet)` returns the row's facet, or `currentFacet` when the state is `ok`. This replaces the unconditional reset at `App.jsx:81-84`.

## 5. The attention strip

A new `AttentionStrip.jsx`, placed under the status bar in both modes. It shows status, not controls, so R4 allows it in Showrunner mode.

- **Summary line** (`role="status"`): "2 frames need attention · 3 to set up" or "6 frames: no problems reported".
- **Alarm list:** one button per alarm frame, e.g. "lobby-left — Player silent · last heard 4 min ago". To-dos sit in a collapsed `<details>`.
- **Clicking an entry:** switches to Wall mode, sets the Surface filter to that frame's `surface_id`, selects the frame, opens `facetFor(state)`, then scrolls the Inspector into view and focuses it.
- **Central line:** the pill reads "Central ok" or "Central: scheduler stale", taken from the `/healthz` body (`app.py:307-343`), or "Central unreachable".
  - If the scheduler is not `ok` or `disabled`, the strip adds: "Player reports may stop being accepted until Central's scheduler recovers." Readiness needs an unexpired offer, and only the scheduler renews offers (`coordination.py:604-610`).
  - If the snapshot is older than three poll intervals, the strip starts with "Central not answering — states below are as of N s ago."
- **Players are not listed.** Per R1, the strip lists frames, not Players. Pending Players show "Last heard …" in the Equipment rail instead.

## 6. Layout and theming (CSS first)

- **Wide screens (≥ 1024 px):** `.console__body` in Wall mode becomes a two-column grid, `minmax(0,1fr) minmax(20rem,26rem)`. The Inspector is `position: sticky`, with `max-height` and `overflow: auto`. It shows "Select a frame" when nothing is selected, so the layout does not jump. `max-width` goes from 1180 px (`index.css:20`) to 1440 px.
- **Narrow screens:** one column, with the Inspector after the plan and tray. The only JavaScript is one effect on selection change that calls `scrollIntoView({block: "nearest"})` and then `focus({preventScroll: true})`. On wide screens this does nothing because the Inspector is already visible.
- **Colour tokens:** about 30 hard-coded hex values collapse into roughly 12 custom properties on `:root`. Examples: `--bg`, `--bg-raised`, `--fg`, `--fg-muted`, `--border`, `--accent`, `--ok`, `--todo`, `--alarm`, `--focus`.
  - Dark stays the default. A `@media (prefers-color-scheme: light)` block overrides the tokens, and `color-scheme: light dark` replaces `index.css:5`.
  - SVG fills use the same tokens.
  - I chose this over `light-dark()` for wider browser support, at the cost of a second block of values.
- **At 390 px:** no horizontal scroll. The fixes are `min-width: 0` on grid and flex children, `flex-wrap` on the header, status bar and token form, and `overflow-wrap: anywhere` on Player and device ids (`device-` plus 64 hex characters cannot wrap). The implementer confirms the actual offenders with the browser test rather than by guessing.

## 7. Polling

- **One poller.** `SnapshotProvider` refreshes every 5 s while the tab is visible and a token is held. 5 s matches the existing calibration overtake poll (`useCalibration.js:16`), and that poll's timer (`useCalibration.js:204-209`) is deleted. Its overtake detection effect still runs on every new snapshot (`useCalibration.js:133-200`).
- **Paused when hidden.** The poller clears its interval on `visibilitychange` to hidden. When the tab becomes visible again it refreshes immediately and restarts, which replaces the ad-hoc listeners at `useSnapshot.js:116-141`.
- **Newest request wins.** Every refresh (poll, mutation, or manual) takes an increasing ticket, and a response is applied only if its ticket is newer than the last one applied. Polls are single-flight: a poll is skipped while another poll is in flight. The fetch timeout is 15 s and the interval is 5 s (`useSnapshot.js:36`), so without this, slow polls would stack up and a slow old poll could overwrite a newer mutation refresh.
- **The draft plane is never touched.** A refresh still replaces Plane A as a whole. `useDraft` re-seeds only on a `frameId` change (`useDraft.js:62-73`), and `useCalibration` re-baselines only per frame (`useCalibration.js:116-127`), so R3 holds by construction. Drag state in the Plan lives in a ref and local state that are not derived from the snapshot.
- **Errors:** unchanged. A network error or 5xx keeps the prior snapshot. A 401 clears the token, which stops the poller (`useSnapshot.js:81-92`).
- **Cost:** three GETs every 5 s per open tab (inventory, runtime, media), because the snapshot stays atomic (design §4a).

## 8. What can go wrong

| Failure | What the operator sees | Guarantee |
|---|---|---|
| A Pi loses power | "Player silent" within 30 s plus one poll | Test (Bead 1) |
| The Player is alive but Central rejects its reports | "Player silent". This is honest ("Central is not accepting it"), and the label says "last heard", not "offline" | Test |
| Central's scheduler stalls, offers expire, and every Player goes silent | The strip explains the likely cause is Central (§5) | Test |
| Central is unreachable | Frozen states plus "as of N s ago". The console never invents silence from browser time | Structural (ages frozen at `read_at`) |
| A display is unplugged after boot | Not detected until the Player restarts. **Stated limit** | None. Deferred, needs a Player-protocol change |
| An old poll lands after a mutation | Dropped by the ticket check | Test (route-delay probe) |
| A draft is being edited during a poll | The draft survives | Structural (R3) plus test |
| The console and the API are different versions | Not possible: `dist` is served by the same app | Deployment |

## 9. Tracer bullet (Bead 1)

A Player's accepted readiness report travels through `player_reports()`, then `last_report_at`, `read_at`, `health.js` (only `player-silent` versus everything else), and finally the tile label "Player silent · last heard N s ago". It is covered by one Playwright test and the backend pytest.

This proves that the chosen signal flows end to end and that ages are Central-relative. **Not included:** the strip, the other states, polling, layout, theming, and failures.

## 10. Beads and file-by-file changes

| Bead | Files | Change |
|---|---|---|
| 1 Tracer | `central/coordination.py` | Add the `player_reports()` read, lock-free, current epoch only. |
| | `central/installation_models.py` | Add `last_report_at` and `read_at`, plus the pure merge helper. |
| | `central/registry.py` | `inventory()` sets `read_at`. |
| | `central/app.py` | The inventory route merges the reports. |
| | `central/console/src/health.js` (new) | `lastHeardAge` and `SILENT_AFTER_SECONDS`; `frameHealth` returns silent or ok. |
| | `central/console/src/Plan.jsx` | The tile dot and label come from `frameHealth`. |
| 2 States and strip | `health.js` | Full precedence table, `facetFor`, `wallAttention(snapshot)`. |
| | `AttentionStrip.jsx` (new) | The strip from §5. |
| | `App.jsx` | Mount the strip; `selectFrame(id, facet)` sets surface, mode and facet; use `facetFor`. |
| | `Plan.jsx` | Drop `CONNECTIVITY_LABEL` and the calibration badge. |
| | `Inspector.jsx` | Add a health header (`role="status"`). |
| | `Commissioning.jsx:471-490` | Rename to "Display at last Player start", with "Detected" / "Not detected". |
| | `BindingFacet.jsx` | Show "Last heard …" for the bound Player. |
| | `EquipmentRail.jsx` | Show "Last heard …" for each pending Player. |
| | `Showrunner.jsx:67` | Use the `frameHealth` label. |
| 3 Polling | `useSnapshot.js` | Add the poller, visibility pause, ticket guard and single-flight; `useHealth` returns `{status, reason}`. |
| | `useCalibration.js` | Delete the interval. |
| | `App.jsx` | Pill text. |
| 4 Layout and theme | `index.css` | Tokens, the light-scheme block, the grid, the 390 px fixes. |
| | `App.jsx` | Wrap the plan column and the Inspector column. |
| | `Inspector.jsx` | Ref, scroll and focus effect, empty state. |
| 5 Failures (cuttable) | `coordination.py`, `installation_models.py` | Add `reported_failures` and the output mapping. |
| | `health.js` | Add row 4. |
| 6 Docs | `docs/runbook.md` (console sections at about lines 350-392) | Liveness, strip, polling cadence, the "at Player start" wording. |
| | This file | History line. |

Also in Beads 2–3: update the existing tests that assert the old labels. They are at `test_operator_wall_browser.py:135,141,512` and `test_operator_commissioning_browser.py:152`.

## 11. Tests and mutation probes

**pytest** (`tests/test_registry.py`, `tests/test_coordination.py`):
- An accepted readiness sets `last_report_at` to the clock value, and `read_at` equals the clock.
- An **unbound** Player gets an offer and `last_report_at` advances.
- After re-enrollment (epoch bump), `last_report_at` is None until a new report arrives.
- A retired Player has no report.
- A rejected readiness (`stale_binding`) leaves `last_report_at` unchanged.
- Bead 5: a failure maps to its output, and an unmapped failure has `output_id=None`.
- The existing check that the inventory response equals `registry.inventory()` (`tests/test_registry.py:388-400`) still passes when there are no reports.

**Playwright** (`tests/browser`). Each test uses `ManualClock` plus `Coordinator.advance()` and `readiness()` to set up state, and `page.clock` to control polling:
1. A silent Player's tile reads "Player silent", and a Player that reports reads "No problems reported".
2. The strip counts alarms separately from to-dos. Clicking a silent frame opens the **Binding** facet; clicking a needs-commissioning frame opens **Commissioning**.
3. A never-commissioned frame reads "Needs commissioning", not in the alarm style. The test asserts the severity class name, not a colour.
4. After fast-forwarding 5 s, the tile reflects a backend change without any operator action. While `visibilityState` is hidden, fast-forwarding 30 s issues no inventory request.
5. A calibration draft survives three polls.
6. A delayed stale poll response does not overwrite a newer Refresh (`page.route` holds the first response).
7. At 1440×900, selecting a tile puts the Inspector in the viewport (`to_be_in_viewport`). At 390×844, the same holds after scrolling, and `scrollWidth <= innerWidth`.
8. Under `emulate_media(color_scheme="light")`, the body background differs from the dark scheme's.

**Mutation probes for the verifier.** Each one must turn the named test red:
- Drop the epoch predicate from `player_reports()` → the re-enrollment test fails.
- Source `last_report_at` from `players.last_seen` → the readiness test fails.
- Swap rows 2 and 5 in the precedence table → Playwright test 2 fails.
- Set `SILENT_AFTER_SECONDS` to `Infinity` → test 1 fails.
- Restore the facet reset in `selectFrame` → test 2 fails.
- Remove the visibility pause → test 4 fails.
- Remove the ticket check → test 6 fails.
- Delete the light-scheme block → test 8 fails.
- Add `white-space: nowrap` to Player ids → test 7 fails.

## 12. Costs, deferrals and questions

- **Deferred:** detecting a display hot-plugged or unplugged after boot. It needs a new Player→Central message, because `OutputReport` is sent only at enrollment.
- **Deferred:** server-side alerting (notifications). With Shape A, the threshold would have to move to the server first.
- **Cost:** the Player backoff (Python) and the console threshold (JavaScript) are linked only by a comment.
- **Cost:** polling adds three GETs every 5 s per open tab.
- **Question 1:** R2 says "central intent **plus real connectivity**". The connectivity was never real (§1). Should R2 be reworded to "…plus last-heard liveness and display detection at Player start"?
- **Question 2:** keep Bead 5 (`failure-reported`) in this slice, or defer it? It is about 60 production lines. The rest of the slice does not depend on it.

## History

- 2026-09-27: first draft. Written after two console audits. The liveness claims were verified against the code at the cited lines. The claim that unbound Players get offers was verified by reading, and Bead 1 adds a test for it.
