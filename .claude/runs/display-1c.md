# Run ledger: roadmap 1c, picture settings on the Display and a Cover fit for Frames

**Branch:** `feat/display-picture-cover` (from `origin/main` at 10c0984), ONE pull request (owner, 2026-10-10). Lane branches `lane/1c-pi`, `lane/1c-render`, `lane/1c-central`, `lane/1c-console`, `lane/1c-docs` start at the F0 commit.
**Rules:** `~/.claude/skills/implementation-workflow/SKILL.md` (binding). An implementer reads **its slice page below, the owner answers, and the design sections the page names**, nothing else.
**Design (accepted):** `/Volumes/Dock/tmp/display-1c/design-draft.md` (its "Owner answers" section binds over its question cards; "For the code architects" and "Reconcile with existing code" are parked notes). Owner answers: `/Volumes/Dock/tmp/display-1c/answers.md` (binding). Review: `review-r1.md`. Code map: `spike-code.md` (file:line on main 10c0984). Hardware spike: `spike-hardware.md`. Gate artifact: https://claude.ai/artifact/GngF5gqPdD6f1PYayGHiZd
**Errata:** `grep -a 'E-1C-' .claude/errata.md`. A page changes only through an erratum and a re-cut: an implementer who hits a wall appends `E-1C-<slice>-<n>` and STOPS.

## The problem in plain words

The owner asked how to change a display's brightness and contrast. Today the only brightness is a Photo Wall dimmer stored with each Frame's position, so moving it re-stamps the Frame's position, and nothing ever touches the display's own settings. 1c stores brightness, contrast, colour temperature and gamma on the Display (so they follow the display when it moves), sets brightness and contrast on the display itself over the HDMI cable's settings channel (DDC/CI) where the display accepts it, and otherwise has the Player adjust the picture it draws; the Picture tab says which, live, with Done and Revert, and Copy to other Frames. In the same pull request the Frame's picture gets a new default fit, **Cover**: the picture is never skewed, it is the smallest upright (or turned, at any angle) rectangle that covers the four corners, and nothing is drawn outside the corners. Today's skewing fit, **Corners**, stays as an explicit choice. "Trim edges" goes away.

## Owner answers (binding; copied verbatim from `/Volumes/Dock/tmp/display-1c/answers.md`)

> # 1c owner answers (gate artifact https://claude.ai/artifact/GngF5gqPdD6f1PYayGHiZd), chat 2026-10-10
>
> Answers are current choices (steers), not requirements.
>
> - q1: "Switch to cover. It's a more sensible default, and skewing transforms or rotations should be an explicit opt in"
>   => every Frame (existing and new) uses Cover. Corners (perspective skew) is an explicit opt-in on the Position tab. New Frames: angle 0. Migration: existing Frames become Cover with their corners as the opening; an existing quarter turn carries over as the angle (READING, asked to confirm; not his words). Each migrated Frame's picture changes; it is not ready until Position is re-checked (cost he accepted). If "not ready" makes a Frame stop showing, that is a finding to report, not a silent choice.
> - q2: "Black/nothing. Remember that a display can have multiple frames, so outside the configured frame is "someone else's territory""
>   => a Frame draws nothing outside its own opening (per-Frame mask in the position pass; never paints over a neighbour Frame on the same Output). Output area outside every Frame's opening is black and stays black after picture adjustment (per-Output mask last in the final pass). Picture adjustment is per Display (per Output). Needs a render test with two Frames on one Output.
> - q3: "Yes. The corners feel like enough" => Trim edges (#99) removed everywhere: contracts, storage (migration drops it), renderer, console, docs.
> - Angle: any angle, quarter-turn buttons + fine control (owner 2026-10-10 "optional rotation angle"); default 0.
> - Earlier (2026-10-10): ONE pull request, no split. Brightness meaning (2026-10-09): "As much as possible" (hardware where the display accepts it).
> - Hardware spike items 2-5 (contrast effect, DDC timing, colour VCPs, standby survival): NOT RUN; owner not yet answered whether to allow a temporary tool load on the test Pi. Bench steps stay NOT RUN in the PR.
>
> Design accepted: design-draft.md in this folder (the "For the code architects" and "Reconcile with existing code" sections are parked notes for code architects). Owner answers section at its top binds over its question cards. Review: review-r1.md. Spikes: spike-code.md, spike-hardware.md.

## Findings for the owner (open; each restated in the PR body)

- **F-1 (q1 cost, measured): a Frame that is not ready shows no photos.** `central/registry.py` `configuration_in` puts only ready Frames in `execution_bindings`; their Outputs alone are in the Player's `enabled_outputs` (`central/coordination.py:202`); the Planner gives media only to enabled Outputs (`coordination.py:278`), and the Player shows its fallback on any other (`player/executor.py:801`). So migrating every Frame to Cover with "not ready until Position is re-checked" **stops the whole wall showing photos from the deploy until each Frame's Position is saved again**. The migration (C1) does what the accepted answer says, in one isolated statement (`UPDATE frames SET position_generation = NULL`); if the owner wants the wall to keep showing with the new Cover picture until each Frame is re-checked, deleting that one line does it (the readiness rule itself is unchanged either way). The PR stays draft until he has seen this.
- **F-2 (q2): one Frame per display today.** Central binds one Frame per Output (`bindings UNIQUE(player_id, output_id)`, migration 001; `PlayerConfiguration` refuses a duplicate Output; the renderer draws one Frame per Output surface). 1c makes every mask per Frame (a Frame never draws outside its opening) and per Output (black outside every opening, last), and proves two Frames on one Output on the renderer's reference (`player/geometry.py output_pixel`, R1). Binding two Frames to one display is a new capability, not in 1c.
- **F-3 (q1 reading still open): a quarter turn carried over as the angle** is the main session's reading, "asked to confirm; not his words". C1 migrates it that way; it is one expression in the migration.
- **F-4 (resolved by the owner, 2026-10-11: E-1C-OWN-1; the cover rectangle keeps the Frame profile's aspect, and nothing below in F-4 holds now): Cover can turn the picture's orientation against the Frame's physical size.** Orientation follows the cover rectangle (design), but today's one copy of the rule "physical and pixel sizes agree on orientation" (`registry._orientation_coherent`) refuses a profile that disagrees. At angles between about 30 and 150 degrees on a landscape Frame, the cover rectangle is portrait, so Position Save refuses `fit_orientation_mismatch` (C3) rather than silently changing the rule. Quarter turns on a physically turned display agree, as today.
- **F-5 (spec):** roadmap 1c's acceptance says "fallback to picture adjustment when refused" and "applied in Display Host"; the accepted design says a refused write is reported, never a silent switch (rule 1), and the Player is the only picture pipeline (F1). X1 corrects the roadmap. The colour temperature test moves from `test_native_math.py` to `test_display_picture_controls.py`: the white balance is worked out on the Pi (operations, not settings, reach the Player).
- **F-6:** Revert of a setting that was "as the display has it" puts back what the display read before the session; a display that gave no read keeps the session's last value (nothing to go back to).

## Lanes, slices and order

Tracer first: **T0** lands the wire (contracts) everything imports, then **P1** is the Pi half of the design's tracer (a Brightness edit's document reaches the controller, which writes VCP 0x10, reads it back and reports `taken` with "On the display"); **C1** is its Central half. Package depth first: `contracts` (T0) before its importers.

| Lane | Slice | What it delivers | Owned paths (outputs its checks write included) | Depends on | Runs concurrently with |
|---|---|---|---|---|---|
| pi | **T0 (tracer, wire)** | Bodies of the 1c contracts: picture parts and their codec, PictureOperations and the picture change text, the cover rectangle | `contracts/node_output.py`, `contracts/node_frame.py`, `contracts/frame_fit.py`, `tests/test_node_output_contract.py`, `tests/test_node_frame_picture.py`, `tests/test_frame_fit.py`, `tests/support/frame_fit_vectors.json` | F0 | nothing (lands first, about 1 h) |
| pi | **P1 (tracer, Pi half)** | The picture controller: DDC/CI 0x10/0x12 probe, write, read-back; one DDC/CI channel per port; newest wins; operations to the shell; the report's picture part; base page line; re-send after a Weston restart | `appliance/display_host/ddc.py`, `appliance/display_host/picture.py`, `appliance/display_host/output_report.py`, `appliance/display_host/power_methods.py`, `appliance/display_host/weston.py`, `appliance/display_host/runner.py`, `appliance/display_host/overlay/render.py`, `appliance/display_host/overlay/client.py`, `tests/node/display/test_display_picture_controls.py`, `tests/node/display/test_display_power_methods.py`, `tests/node/display/test_node_display_runner.py`, `tests/node/display/test_display_overlay_render.py` | T0 | R1, R2, C1, C2, C3, P2 |
| pi | **P2** | Frame protocol v4 in C: the shell's `picture` op and event, `tag_picture`, `presented` echoes the picture change, the diagnostic `brightness` event, re-send on bind; the frame client's two v4 functions; both sides bind 4 | `appliance/display_host/native/shell.c`, `appliance/display_host/native/frame-client.c`, `appliance/display_host/native/frame-client.h`, `appliance/display_host/native/photo-wall-frame-v1.xml`, `tests/test_node_display_native.py`, `tests/native_display_smoke.py` | T0 | P1, R1, C1, C2, C3 |
| render | **R1** | The scalar reference: one matrix per fit, per-Frame mask, final pass with the per-Output mask last, Photo fit renamed, trim gone from the reference | `player/geometry.py`, `tests/test_native_math.py` | T0 | P1, P2, C1, C2, C3 |
| render | **R2** | The native renderer: shaders on the one matrix and both masks, picture operations from the shell, the picture change in the tag, trim and gain gone from the renderer; renderer pixel tests in CI | `player/native.py`, `player/rendering.py`, `player/wayland_frames.py`, `player/calibration_trial.py`, `player/executor.py` (only `effective_calibration` callers if they change), `tests/native_renderer_pixel_harness.py`, `tests/native/` (deleted), `scripts/run_display_harness.py`, `scripts/release_plan.py`, `tests/test_release_plan.py` (if it pins the suite's paths), `tests/test_executor.py` | R1, P2 (its ctypes calls bind P2's two functions) | P1, C1, C2, C3 |
| central | **C1 (tracer, Central half)** | Migration 075; picture settings on the Display; first-probe placement; the document's picture part with its own picture change; the report's picture part judged; every Frame migrated to Cover; trim and the V1 calibration route removed | `central/migrations/075_display_picture.sql`, `central/displays/picture.py`, `central/displays/model.py`, `central/infra/display_store.py`, `central/registry.py`, `central/app.py`, `central/installation_repository.py`, `tests/test_display_picture_settings.py`, `tests/integration/test_central_display_picture.py`, `tests/test_registry.py`, `tests/test_node_calibration.py`, `tests/test_coordination.py`, `tests/test_node_display.py`, `tests/browser/test_operator_showrunner_browser.py` (only its V1 calibration call, if any) | T0 | P1, P2, R1, R2 |
| central | **C2** | The picture session (Live model), Done and Revert, Copy, the eight routes; the shared live-session rules, with Position's trial migrated onto them | `central/live_session.py`, `central/displays/picture.py`, `central/displays/views.py`, `central/displays/ports.py`, `central/display_routes.py`, `central/infra/display_store.py`, `central/fleet/node_calibration.py`, `central/app.py`, `tests/test_display_picture_settings.py`, `tests/test_live_session.py`, `tests/test_node_calibration.py` | C1 | P1, P2, R1, R2 |
| central | **C3** | Frame fit through trial and Save; the profile shape written by Save in Cover; the Player configuration carries the fit | `central/fleet/node_calibration.py`, `central/registry.py`, `central/displays/model.py`, `tests/test_frame_fit_settings.py`, `tests/test_node_calibration.py` | C2, T0 | P1, P2, R1, R2 |
| console | **K1** | Picture tab (Live: four sliders, where each acts, Done, Revert, Copy) | `central/console/src/pictureApi.js`, `central/console/src/pictureSession.js`, `central/console/src/domain/frame-picture.tsx`, `central/console/src/domain/frame-picture.stories.tsx`, `central/console/src/pages/frame-page.tsx`, `central/console/src/liveAdjustment.js`, `central/console/src/useDraft.js`, `tests/browser/test_frame_picture_browser.py`, `tests/browser/test_console_frame_page_browser.py`, `tests/browser/catalog-baselines/` (new stories), `central/console/dist/` | C3 (the CENTRAL lane tip) | X1 |
| console | **K2** | Position tab: fit choice (Cover default, Corners opt-in), angle presets and fine control; Trim removed | `central/console/src/domain/frame-position.tsx`, `central/console/src/domain/frame-position.stories.tsx`, `central/console/src/patterns/quad-editor.tsx`, `central/console/src/patterns/quad-editor.stories.tsx`, `central/console/src/useDraft.js`, `central/console/src/liveAdjustment.js`, `central/console/src/convex.js`, `central/console/src/projection.js`, `central/console/src/pages/frame-page.tsx`, `tests/browser/test_frame_fit_browser.py`, `tests/browser/test_console_frame_page_browser.py`, `tests/test_console_convex.py` (new; runs `convex.js` under Node 22 against the shared vectors, as `tests/test_console_flow.py` runs console modules), `tests/browser/catalog-baselines/`, `central/console/dist/` | K1 | X1 |
| docs | **X1** | Design homes updated to what landed | see its page | drafts at once from the pages; final pass after K2, P2, R2 | everything |
| integrate | **I1** | Retire the additive shapes (the tripwire); the real-Weston end-to-end pixel test controller -> shell -> Player; seams | see its page | every other slice | nothing (last) |

**Fan-out.** T0 lands first (contracts are imported by every lane). Then the pi lane (P1 and P2 at once: Python against C), the render lane (R1, then R2) and the central lane (C1, then C2, then C3) run concurrently in their own worktrees. R2 waits for P2 only to run its real-library path; it codes against the frozen C signatures in `frame-client.h` from the start. The console lane starts from the central lane's tip (after C3) and runs K1 then K2 (both edit `frame-page.tsx`, `liveAdjustment.js`, `useDraft.js` and build `central/console/dist/`). X1 drafts from these pages at once and finishes after K2, P2 and R2. I1 runs last. Only T0 touches `contracts/` before I1; only C1 adds a migration; `central/console/` belongs to the console lane alone.

**Seams named now (the end fix's if they open):** (a) the picture change: C1 writes `picture_change` -> P1 carries it in `PictureOperations` -> P2's shell forwards -> R2 tags it -> P2's shell echoes it in `presented` -> P1 reports `presented_change` -> C2's `picture_status`; I1's end-to-end test drives the whole chain. (b) `Calibration.fit`: C1 writes it, R2 draws it through R1's `fit_of`, K2 edits it; until I1 a record without `fit` draws as Corners. (c) Brightness: C1 moves `gain` to the Display; R2 stops reading `gain`; between R2 and C1 landing on the PR branch a Frame's old gain is not applied (intermediate, inside the PR).

**Land command (every slice).** From the lane's worktree, with `<owned>` = the slice's owned paths that exist:
```
git add -A -- <owned> && git commit -m '<msg>' -- <owned>
git pull --rebase origin feat/display-picture-cover && git push origin HEAD:feat/display-picture-cover
```
A commit that meets a sibling's `index.lock` waits and retries. After a lane's last slice, `git status --porcelain` is empty; anything left is a write outside an owned list and stops the run. Slice agents do not edit this ledger; the run's closing step fills the outcome table.

**Bench steps (NOT RUN in the PR; need a deployed release on the test Pi, which runs v0.24.1, and the owner's answer on a temporary tool load):** set brightness from the console and read it back with `ddcutil getvcp 10`; check Cover and a 7-degree and a 90-degree rotation on the test monitor; whether contrast (VCP 0x12) changes the picture; DDC/CI write timing and writes per drag at 2 per second (EEPROM wear); colour VCPs (0x14, 0x0C); whether the Player survives standby; Weston's journal for a hot-plug blip after a 0x10 write.

## Environment and gates

- Checkout: each lane's own worktree (`isolation: "worktree"`); `.venv/bin/python` after `UV_CACHE_DIR=/Volumes/Dock/tmp/uv-cache uv sync --frozen`; revert `uv.lock` if anything rewrites it. No slice changes a dependency.
- Console: `PATH=$HOME/.nvm/versions/node/v22.23.2/bin:$PATH` before any test or npm run.
- pytest `--basetemp=/tmp/pw1c/<slice>/<gate>` (short: AF_UNIX socket paths).
- `PHOTO_WALL_NATS_SERVER=/Volumes/Dock/tmp/nats/nats-server-v2.15.0-darwin-arm64/nats-server`.
- New integration tests that need the database are named `tests/integration/test_central_*.py` (CI `db` leg, with NATS); bus-only ones `tests/integration/test_node_bus_*.py` (`node-bus` leg).
- Locally each slice runs its changed packages' tests, ruff, lint-imports, check_docs, and the console typecheck and lint when it touches the console; CI (`pipeline.yml`) is the full gate. C code (P2) and the native renderer (R2) are judged by CI only (`linux-media`, node-pid1 `display-harness`): this Mac has no Weston or wayland-scanner.

| Gate | Command |
|---|---|
| static | `.venv/bin/python -m ruff check .` · `.venv/bin/lint-imports` · `python3 scripts/check_docs.py` |
| unit | `PHOTO_WALL_TEST_REQUIRE_DATABASE=1 .venv/bin/python -m pytest -q -m "not db and not browser" -n 4 --dist worksteal <paths>` |
| bus | `PHOTO_WALL_NATS_SERVER=… .venv/bin/python -m pytest -q tests/integration -k node_bus -n 4` |
| db | `docker compose -f tests/integration/compose.test-database.yml up -d --wait`, then `.venv/bin/python scripts/test_local.py -q -m db -n 4 --dist loadgroup <paths>` |
| browser | `PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q tests/browser/<file> -n 4 --browser chromium` (built console) |
| console | `npm --prefix central/console run typecheck` and `run lint`; `npm --prefix central/console run build` |

**Preflight at F0 (2026-10-10, this branch after the freeze):** ruff 0; lint-imports 22 kept, 0 broken (one new: "Display Host never computes a Frame's geometry"); check_docs 0; unit tier 3885 passed, 133 skipped, 1 failed: `tests/node/apps/test_player_device_grants.py::test_start_spawns_the_player_with_this_nodes_grants` (`time.CLOCK_BOOTTIME` is Linux-only, `appliance/kernel/clock.py:14`; fails identically on `origin/main` here, A/B run; no F0 file on its path). DB tier for `test_display_identity`, `test_display_changed_readiness`, `test_display_power_requests`, `test_node_calibration`, `test_registry`, `test_node_acceptance`, `test_coordination`: 99 passed. `test_node_bus_display_line`, `test_central_display_documents`, `test_central_display_power`: passed. Browser, console, linux-media, display-harness, the Debian build's import check: NOT RUN at F0 (F0 changes no behaviour; the C and XML edits are runtime-neutral because shell and frame client both still bind version 3).

**Budget gates.** Per slice: 90 min wall-clock (120 for P1, P2, R2, C1, C2), at most 8 agents. Feature: 12 h wall-clock and 8 M tokens; warning at 80 %, stop after the current slice at 100 %. At most two fix cycles per gate, then a pushed `wip/1c-<slice>` branch and stop. A course-correction architect pass after the fifth implementer and before the end review. Models pinned by id: `claude-opus-5-5` for T0, P1, P2, R1, R2, C1, C2, C3, K1, K2, I1; `claude-sonnet-5-5` allowed for X1 (owner, 2026-10-09).

**End (once, over BASE..HEAD):** CI watcher on the PR; verifier (each page's mutation probes, conformance to the pages); review lenses in parallel with a **security lens** (a migration that rewrites every Frame, new operator routes, a new device write path) and a domain lens on the picture rules and the fit geometry; one fix agent. The PR then goes ready with before/after screenshots (dark and light) of the Picture and Position tabs, after the owner has seen F-1.

---

## F0: the freeze (this commit; code architect)

Interfaces as compiling code; new bodies `NotImplementedError`; additive, so every existing consumer stays green.

| File | What is frozen |
|---|---|
| `contracts/frame_fit.py` (new) | `Point`, `Corners`, `OutputSize`, `FULL_SCREEN`, `CONVEX_EPSILON`, `convex_clockwise` (real: moved verbatim from `Calibration`'s validator), `CoverRectangle` (+`landscape`), `cover_rectangle` |
| `contracts/models.py` | `CornersFit`, `CoverFit`, `FrameFit` (discriminated on `mode`), `Calibration.fit: FrameFit | None = None` with the construction check "with `fit`, the retiring fields keep their defaults" (`calibration_retiring_fields_with_fit`), and a serializer that leaves a record without `fit` byte-identical |
| `contracts/node_output.py` | `ActsOn`, `AS_THE_DISPLAY_HAS_IT`, `PictureValue`, `WriteResult`, `PictureSetting`, `OutputPicture`, `SettingReport`, `PictureReport`; constants `PERCENT_MAX`, `KELVIN_*`, `GAMMA_TENTHS_*`; `OutputDocument.picture_change`, `.picture` and `OutputReport.picture` (default None). The document's encoding now carries `"picture":null,"picture_change":null` (the contract test's canonical bytes updated); every Output's change number rises once at deploy |
| `contracts/node_frame.py` | `PictureOperations`, `neutral_operations`, `encode_picture_operations`, `decode_picture_operations`, `picture_change_text`, `parse_picture_change`, `PICTURE_OPERATIONS_BYTES`, the operation ranges, `NO_PICTURE_CHANGE` |
| `appliance/display_host/native/photo-wall-frame-v1.xml` | `pw_frame_manager_v1` version 4: event `picture(output_id, operations)`, request `tag_picture(surface, grant_id, frame_tag, picture_change)`; `pw_diagnostic_manager_v1` version 4: event `brightness(output_id, text)` |
| `appliance/display_host/native/frame-client.h` | `pw_frame_client_picture`, `pw_frame_client_tag_picture_next_commit` (declared; P2 defines them) |
| `appliance/display_host/ddc.py` (new) | `VcpReading`, `DdcChannel` (`bus`, `get`, `get_continuous`, `set`) |
| `appliance/display_host/picture.py` (new) | VCP codes, timings, `kelvin_multipliers`, `operations_for`, `vcp_for`, `percent_of`, ports `PictureDdc`, `PictureShell`, `PictureReports`, `DdcPictureAdapter`, `PictureController` |
| `appliance/display_host/output_report.py` (new) | `OutputReports` (`put_report`, `emit_attempt`, `put_picture`) |
| `player/geometry.py` | `Linear`, `fit_of`, `output_to_picture`, `inside_opening`, `photo_rect`, `picture_source_uv`, `final_pixel`, `output_pixel` |
| `central/live_session.py` (new) | `StaleSequence`, `Lease` (+`live`), `begin_lease`, `touched`, `next_sequence` |
| `central/displays/picture.py` (new) | `PICTURE_IDLE_SECONDS`, `Setting`, `PLACEABLE`, `PictureStatus`, `PictureRefused`, `StoredSetting`, `DisplayPicture`, `PictureSession`, `default_picture`, `placed_at_first_probe`, `effective_picture`, `reverted`, `edited`, `placed`, `output_picture`, `picture_status`, `copied` |
| `central/displays/model.py` | `cover_profile` |
| `central/displays/views.py` | `PictureSettingView`, `PictureSessionView`, `FramePictureView`, `PictureEdit`, `PicturePlace`, `PictureSequence`, `PictureCopyRequest`, `CopyOutcome`, `PictureCopyResult` |
| `central/displays/ports.py` | `PictureQueries.frame_picture`; `PictureCommands.begin_picture`, `edit_picture`, `place_picture`, `keep_picture_alive`, `picture_done`, `revert_picture`, `copy_picture` |
| `central/display_routes.py` | `mount_picture_routes` and its eight route declarations (not mounted until C2), `PICTURE_SESSIONS` |
| `pyproject.toml` | import-linter: "Display Host never computes a Frame's geometry" (`appliance` may not import `contracts.frame_fit`) |

### Route table (the console lane codes against it)

| Method | Path | Body | Answer | Refusals | Mounted by |
|---|---|---|---|---|---|
| GET | `/v1/operator/frames/{frame_id}/picture` | | `FramePictureView` | 404 `unknown_frame` | C2 |
| POST | `/v1/operator/frames/{frame_id}/picture-sessions` | | 201 `PictureSessionView` | 404 `unknown_frame`; 409 `frame_unbound`, `no_display`, `session_open` | C2 |
| POST | `/v1/operator/picture-sessions/{session_id}/edits` | `PictureEdit {setting, value, expected_sequence}` | `PictureSequence` | 404 `unknown_session`; 410 `session_ended`; 409 `stale_sequence`, `not_placed_yet`; 422 `out_of_range` | C2 |
| POST | `/v1/operator/picture-sessions/{session_id}/places` | `PicturePlace {setting, acts_on, expected_sequence}` | `PictureSequence` | 404; 410; 409 `stale_sequence`, `display_did_not_answer` | C2 |
| POST | `/v1/operator/picture-sessions/{session_id}/keepalive` | | `PictureSessionView` | 404; 410 | C2 |
| POST | `/v1/operator/picture-sessions/{session_id}/done` | | 204 | 404; 410; 409 `not_presented_yet` | C2 |
| DELETE | `/v1/operator/picture-sessions/{session_id}` | | 204 (Revert) | 404; 410 | C2 |
| POST | `/v1/operator/frames/{frame_id}/picture-copies` | `PictureCopyRequest {to_frames}` | `tuple[PictureCopyResult, ...]` | 404 `unknown_frame`; 409 `no_display` | C2 |
| POST | `/v1/operator/frames/{frame_id}/calibration` (V1) | | removed | | C1 |
| existing trial routes (`…/calibration-trials…`) | | `calibration` now carries `fit` | unchanged | `fit_orientation_mismatch` 409 on `save` in Cover; `output_size_unknown` 409 when the bound Output reported no size | C3 |

Every route takes the operator `admin` dependency. Refusals answer `{"error": code}` at the status, as `apiWrite.js` reads.

---

## T0 (tracer, wire): the 1c contracts (lane pi, first)

**Owned:** see the lane table.
**Design:** design-draft "Wire and record additions", "Frame fit and Cover" (the Cover table), "Final pass order"; this ledger's F0 table.
**Frozen page:** every F0 signature in `contracts/` exactly as frozen; bodies exactly as their docstrings say, with the 1b codec's helpers (`_require`, `_count`, `_fields`, `_plain`, `_encode`, `_decode`) and no new helper that duplicates one:
- `PictureSetting`, `OutputPicture`, `SettingReport`, `PictureReport` `__post_init__` as their docstrings; `OutputDocument.__post_init__` adds "both picture fields None or both set"; `OutputReport.__post_init__` adds "picture None or a PictureReport". Field names on the wire are the dataclass field names; `value` encodes as an int or the string `"as-the-display-has-it"`.
- `decode_output_document` and `decode_output_report` build the nested picture parts (`ActsOn(...)`, `WriteResult(...)` or None).
- `contracts/node_frame.py`: the codec of `PictureOperations` is canonical JSON (`json.dumps(sort_keys=True, separators=(",", ":"), allow_nan=False)`), decoded through `contracts.strict_json.loads_object(max_bytes=PICTURE_OPERATIONS_BYTES)`; `rgb_multipliers` a 3-list. `picture_change_text(None) == "0"`, `picture_change_text(17) == "17"`, `parse_picture_change("0") is None`, `parse_picture_change("017")` raises.
- `contracts/frame_fit.py`: `cover_rectangle` as its docstring (u = (cos r, sin r), v = (-sin r, cos r) in y-down pixels; width and height the projection spreads; centre the spreads' midpoints); `CoverRectangle.landscape` = `width >= height`.
- `tests/support/frame_fit_vectors.json`: the shared vectors (convex and folded corner sets; cover rectangles at 0, 7, 45, 90, 135 degrees for a 1920x1080 output) that `tests/test_frame_fit.py` reads and K2's console check reads too (one source of test vectors for `convex.js`).
**Acceptance (unit leg):**
- `tests/test_node_output_contract.py`: a document and a report with picture parts round-trip; byte-identical across field orders; refusals: picture_change without picture, value 101, value "50", value True, kelvin 6550, gamma 27, a `SettingReport` with `read` while `picture-adjustment`, `presented_change` 0.
- `tests/test_node_frame_picture.py`: operations round-trip; refusals: largest multiplier 0.99, a multiplier 0, light 2.01, gamma exponent 0.4, NaN, past 512 bytes, an unknown field; `neutral_operations` is (1,1,1),1,1,1; picture change text both ways.
- `tests/test_frame_fit.py`: at 0 degrees the cover rectangle of an axis-aligned opening is that opening; at 90 degrees on a 16:9 full-screen opening it is 1080 wide and 1920 tall in pixels; at 45 degrees on a square opening `landscape` is True; every opening corner lies inside its cover rectangle (all five angles, tolerance 1e-9) and at least two touch its edges (tight); the centre is the spreads' midpoint, not the corners' average (an asymmetric quad); worked in pixels, not fractions (a 7-degree case whose fraction-space answer differs); a folded quad is `cover_corners`.
**Mutation probes:** centre on the corners' average -> the asymmetric case fails; compute in fractions -> the pixels case fails; drop the "both or neither" picture check -> its refusal fails; accept the largest multiplier below 1 -> its refusal fails.
**Land:** `feat(contracts): picture parts of the Output document, picture operations, the cover rectangle (1c T0)`.

## P1 (tracer, Pi half): the picture controller (lane pi)

**Owned:** see the lane table. Siblings share the lane: P2 runs at the same time in `native/`; keep to these paths.
**Design:** design-draft rule 1, "Contracts" rows "Pi › Display controller" and "Pi › Shell", "What the tab and the base page say", PARKED bench notes; spike-hardware.md; `.claude/skills/display-control/SKILL.md` (VCP 10/12 on the test monitor).
**Frozen page:** `ddc.py`, `picture.py`, `output_report.py` exactly as frozen in F0; their docstrings are the contract. Additions:
- `power_methods.DdcCiAdapter.__init__(self, channel: DdcChannel) -> None` (the `Runner`, `drm_root` and `cache_dir` move to `DdcChannel`; `DdcChannel` is imported only under `TYPE_CHECKING` there, since `ddc.py` imports `Runner` and `DDC_CACHE` from `power_methods`). Its D6 reads and writes go through `channel.get` / `channel.set`; behaviour otherwise unchanged (the 1b tests stay green with the channel injected).
- `weston.WestonBackend.picture(self, operations: PictureOperations, *, timeout: float) -> bool` sends the control op `{"op": "picture", "output_id": <id>, "output": <key>, "operations": <encode_picture_operations(...)>, "brightness": <text>}`, where the text comes from `picture.brightness_line(setting: PictureSetting, read: int | None) -> str` (add it to `picture.py`): "Brightness: on the display · 60 %", "Brightness: Photo Wall picture adjustment · 40 %", "Brightness: as the display has it", "Brightness: on the display · the display did not take it". The backend's dispatch hands the `presented` event's `"picture_change"` (P2) to `PictureController.presented` through `parse_picture_change`, and an `output` event for a created Output to `PictureController.shell_output`.
- `overlay/render.render_slate(name, width, height, reason, testing, brightness: str = "") -> DrawList`: the sixth line is `brightness`, or nothing when empty ("Panel brightness unavailable" is deleted, F8); `overlay/client.py` keeps the latest `brightness` event's text per Output and redraws the slate on it.
- `runner.main()`: one `DdcChannel(run_tool)` shared by `DdcCiAdapter` and `DdcPictureAdapter`; `OutputReports(BusReportSink(session))` is the power controller's sink and the picture controller's reports; `document_feed` hands each decoded document to both controllers; every Output observation goes to both (`PictureController.output`).
**Acceptance (unit leg; fake `ddcutil` on a temp PATH through the real `run_tool`, a fake shell, a fake clock, as `test_display_power_methods.py`):** `tests/node/display/test_display_picture_controls.py`:
1. Brightness 60 on the display (max 100) writes `setvcp 10 60`, reads back 60 and reports `taken`, `read` 60; with max 255 it writes 153.
2. A display that reads back another value reports `not-taken`; one that does not answer reports `no-answer`; in both the setting's `acts_on` in the report stays `display` and the operations' light factor stays 1.
3. Unset (as the display has it) writes nothing (no `setvcp` in any argv) and sends the neutral operation.
4. Brightness 40 as picture adjustment writes nothing over DDC/CI and sends light factor 0.8; contrast 25 sends slope 0.5; gamma 24 sends exponent 24/22; 6500 K sends (1, 1, 1); 3000 K and 9300 K send multipliers matching Charity's table (vendian.org, CIE 1931 2-degree, D65 white, normalised at 6500 K) within 1e-3, largest exactly 1.
5. Newest wins: with a fake `ddcutil` that takes 2 s per write, five documents (picture changes 2..6) arrive in 1 s; the writes made are for change 2 and change 6 only.
6. One command at a time per port: a power D6 write and a brightness write on the same port never overlap (the fake records start and end times); two ports do overlap.
7. Re-send: `start()` sends every Output's operations; `shell_output("HDMI-A-1")` sends that Output's again; no DDC/CI write is repeated by either.
8. A re-read every 60 s while a value is set: a display read 35 after a write of 60 reports `read` 35 and writes nothing (the next picture change writes again).
9. `presented`: a `presented` echo of change 6 reports `presented_change` 6; a new change whose operations equal the last presented ones reports it presented at once.
10. A document with picture None does nothing; a power change alone (`change` rises, `picture_change` the same) writes no VCP.
Also: `test_display_power_methods.py` stays green with `DdcCiAdapter(DdcChannel(fake))`; `test_node_display_runner.py` pins that one channel feeds both adapters; the overlay test pins the brightness line and that "Panel brightness unavailable" is gone.
**Mutation probes:** write when acts_on is picture adjustment -> case 4 fails; queue instead of replace -> case 5 fails; drop the port lock -> case 6 fails; write on a power change -> case 10 fails; report `taken` from the tool's exit status -> case 2 fails; switch acts_on on a failed write -> case 2 fails.
**Land:** `feat(display): brightness and contrast on the display over DDC/CI, picture operations to the shell (1c P1)`.

## P2: frame protocol v4 in the shell and the frame client (lane pi)

**Owned:** see the lane table. Siblings share the lane: P1 runs at the same time in Python; keep to these paths.
**Design:** design-draft "Contracts" row "Pi › Shell" (it forwards, never interprets); the XML descriptions frozen in F0.
**Frozen page:** the XML as frozen in F0 (P2 may only fix wording). In `shell.c`:
- `wl_global_create(..., &pw_frame_manager_v1_interface, 4, ...)` and `bind_frame` caps at 4; the diagnostic global and `bind_diagnostic` cap at 4.
- Control op `picture`: refuses unless `operations` is a string of at most 512 bytes and `brightness` a string of at most 128; stores both per Output (`char picture_operations[513]`, `char brightness[129]`); sends `picture(output_id, operations)` to every version-4 frame manager resource of the Output's app client, and `brightness(output_id, text)` to the diagnostic resource. On a frame manager bind at version 4 and on a diagnostic bind, every Output's stored values are sent. It never parses `operations`.
- Request `tag_picture`: as `tag`, plus `picture_change` (a token of at most 20 characters, digits only) stored beside the pending tag and carried into the feedback; the `presented` event gains `"picture_change": <text>` (the text, or `null` for a buffer tagged with v3 `tag`).
- An Output (re)created emits the existing `output` event (P1 re-sends on it).
In `frame-client.c`: bind at `version >= 4 ? 4 : version` (and still refuse below 3); a `picture` listener stores the newest operations per Output; `pw_frame_client_picture` and `pw_frame_client_tag_picture_next_commit` as declared in `frame-client.h`.
**Acceptance:** `tests/test_node_display_native.py` (CI `linux-media`, native image): the control op `picture` reaches a v4 test client as the `picture` event verbatim; a client binding after it receives the stored operations; `tag_picture` with "6" yields a `presented` event with `"picture_change": "6"`; a v3 `tag` yields `null`; 513 bytes of operations are refused. `tests/native_display_smoke.py` (node-pid1 `display-harness`): the diagnostic client receives `brightness` and the slate pixels change where the line is drawn.
**Mutation probes:** parse `operations` in the shell -> a non-JSON 10-byte operations string (allowed) is refused and the verbatim case fails; drop the re-send on bind -> the late-client case fails; leave the cap at 3 -> every v4 case fails.
**Land:** `feat(display): frame protocol v4 carries picture operations and the presented picture change (1c P2)`.

## R1: the scalar reference (lane render)

**Owned:** `player/geometry.py`, `tests/test_native_math.py`.
**Design:** design-draft "Frame fit and Cover", "Final pass order", design rule 2; owner q2 (two Frames, black outside); `contracts/frame_fit.py`'s docstring.
**Frozen page:** the R1 functions of `player/geometry.py` exactly as frozen in F0. `output_to_picture` for Cover is the inverse of: rotate by `rotation` clockwise, scale to the cover rectangle's width and height (pixels), move to its centre, all in output pixels and then divided by the output size; for Corners it is `inverse(homography(corners))` followed by the quarter-turn undo, folded into one matrix (so `inverse_rotation` becomes a matrix constant). `photo_rect` is today's `cover_rect` with the crop parameter gone; `cover_rect` and `source_uv` stay until I1 (R2 stops calling them). `compose_pixel`'s `gain` parameter stays until I1; R1 adds `final_pixel` beside it.
**Acceptance (unit leg; `tests/test_native_math.py`):**
1. Corners equals today: for six corner sets and the four quarter turns, `picture_source_uv` with a `CornersFit` equals `source_uv` with today's `Calibration` (crop full) at 200 sampled points, within 1e-9.
2. Cover is tight and unskewed at 0, 7, 45, 90, 135 degrees on a 1920x1080 output: every opening corner maps inside the picture's unit square; at least one corner on each of two adjacent sides maps to within 1e-9 of that edge; the matrix's linear part, scaled to pixels, has orthogonal columns of equal length ratio to the picture (no skew).
3. Per-Frame mask: a point inside the cover rectangle but outside the opening gives `picture_source_uv` None.
4. Two Frames on one Output (owner q2): two openings side by side on one 1920x1080 output, Frame A red, Frame B green, with contrast slope 0.5: `output_pixel` is red inside A, green inside B, and exactly opaque black between and around them; neither colour appears inside the other's opening.
5. Final pass order: with operations (0.9, 0.95, 1), light 1.2, slope 0.8, exponent 24/22, `final_pixel` equals the hand-tabulated values (a table in the test, worked by hand) for linear inputs 0, 0.18, 0.5, 1; with slope 0.5 and `inside=False` the pixel is (0, 0, 0, 1) (the mask after the contrast lift); neutral operations equal today's `compose_pixel` with gain 1.
**Mutation probes:** mask before the contrast step -> case 5's black-outside fails; compute Cover in fractions -> case 2's no-skew fails; centre on the corners' average -> case 2's tight case fails; drop the per-Frame mask -> cases 3 and 4 fail.
**Land:** `feat(player): one matrix per Frame fit, masks per Frame and per Output, the final pass reference (1c R1)`.

## R2: the native renderer (lane render)

**Owned:** see the lane table.
**Design:** design-draft "Final pass order", rule 2, F4, F5; `.claude/runs/display-1b.md` P1a for the harness shape.
**Frozen page:**
- `_LAYER`: uniforms `mat3 output_to_picture` (from `output_to_picture(fit_of(calibration), (width, height))`), `vec2 opening[4]`; discard outside the opening (half-planes, as `inside_opening`), then outside the picture's unit square; `rotation` and `crop` uniforms are gone; per-layer `cover` is `photo_rect(profile aspect, …)` renamed in the shader to `photo`. The commissioning grid reads `photo` = (0, 0, 1, 1).
- `_FINAL`: uniforms `vec3 rgb_multipliers`, `float light_factor`, `float contrast_slope`, `float gamma_exponent`, `vec2 opening[4]`; the order of `final_pixel`, the mask last; `gain` is gone.
- `_Surface` keeps `operations: PictureOperations | None` (None: neutral). `_render` reads `frames.picture(output_id)` each render (like `trial`), redraws when it changed, and tags with `tag_rendered_buffer(..., picture_change=operations.picture_change if operations else None)`.
- `wayland_frames.WaylandFrames.picture(self, output_id: str) -> PictureOperations | None` (ctypes over `pw_frame_client_picture`, decoded by `decode_picture_operations`; an undecodable one is logged and ignored) and `tag_rendered_buffer(self, gdk_window, grant, frame_tag, primitives=None, *, picture_change: int | None = None) -> None` (always `pw_frame_client_tag_picture_next_commit` with `picture_change_text(picture_change)`).
- `calibration_trial.TrialState.apply`: the trial's primitives are the fit's corners (clamped), both modes; the trial's `Calibration` may carry `fit`.
- `rendering.OutputComposition`, `Renderer` and `PresentationResult`: unchanged.
- `tests/native/` (smoke.py, its Dockerfile and script, run by no CI job, F5) is deleted; its assertions move into `tests/native_renderer_pixel_harness.py`, which `scripts/run_display_harness.py`'s `run_argv` runs after `native_player_resource_harness.py`, and which `scripts/release_plan.py`'s node-pid1 suite lists.
**Acceptance (CI node-pid1 `display-harness`, about 40 s added; `PASS <name>` per case, the real `NativeRenderer` under its own headless Weston, `glReadPixels` against R1's reference within 0.015, a fake `_frames` that serves operations as in 1b P1a case 4):**
1. `cover_at_7_degrees_matches_the_reference` (and 0, 90): sampled pixels equal `picture_source_uv` + `final_pixel`.
2. `outside_the_opening_stays_black_at_low_contrast`: slope 0.5, a Cover fit with an opening smaller than the output: every sampled pixel outside the opening is (0, 0, 0).
3. `corners_fit_equals_today`: a skewed `CornersFit` at 90 degrees equals the old path's pixels (computed by `source_uv`).
4. `operations_change_the_picture_and_the_tag`: a new operations value with change 7 redraws, and the tag call carries "7".
5. `no_trim_and_no_gain_uniforms`: the linked programs have no `crop`, `gain` or `rotation` uniform.
`tests/test_executor.py` stays green.
**Mutation probes:** mask before the contrast step in `_FINAL` -> case 2 fails; omit the picture change from the tag -> case 4 fails; keep reading `gain` -> case 5 fails.
**Land:** `feat(player): Cover and Corners through one matrix, picture operations in the final pass, black outside every opening (1c R2)`.

## C1 (tracer, Central half): picture settings on the Display (lane central)

**Owned:** see the lane table.
**Design:** design-draft rules 1 and 3, "Data flow", "Wire and record additions", the migration sentence under "Final pass order"; owner q1, q3; F-1, F-3 above.
**Frozen page:**
- Migration `075_display_picture.sql` (forward-only; no trigger, no stored procedure):
  ```sql
  ALTER TABLE displays
    ADD COLUMN brightness_acts_on TEXT NULL CHECK (brightness_acts_on IN ('display','picture-adjustment')),
    ADD COLUMN brightness INTEGER NULL CHECK (brightness BETWEEN 0 AND 100),
    ADD COLUMN contrast_acts_on TEXT NULL CHECK (contrast_acts_on IN ('display','picture-adjustment')),
    ADD COLUMN contrast INTEGER NULL CHECK (contrast BETWEEN 0 AND 100),
    ADD COLUMN colour_temperature_kelvin INTEGER NOT NULL DEFAULT 6500
      CHECK (colour_temperature_kelvin BETWEEN 3000 AND 9300 AND colour_temperature_kelvin % 100 = 0),
    ADD COLUMN gamma_tenths INTEGER NOT NULL DEFAULT 22 CHECK (gamma_tenths BETWEEN 18 AND 26);
  -- NULL value = as the display has it; NULL acts_on = not placed yet (no probe seen).
  CREATE TABLE picture_sessions (
    id UUID PRIMARY KEY,
    display_id UUID NOT NULL UNIQUE REFERENCES displays(id) ON DELETE CASCADE,
    frame_id TEXT NOT NULL REFERENCES frames(id) ON DELETE CASCADE,
    sequence BIGINT NOT NULL CHECK (sequence >= 1),
    draft JSONB NOT NULL,                      -- DisplayPicture, the stored columns' names
    before_brightness INTEGER NULL CHECK (before_brightness BETWEEN 0 AND 100),
    before_contrast INTEGER NULL CHECK (before_contrast BETWEEN 0 AND 100),
    touched TEXT[] NOT NULL DEFAULT '{}',
    idle_until DOUBLE PRECISION NOT NULL,
    created_at DOUBLE PRECISION NOT NULL);
  ALTER TABLE output_documents
    ADD COLUMN picture_change BIGINT NULL CHECK (picture_change >= 1),
    ADD COLUMN picture_digest TEXT NULL;
  -- gain -> brightness (design): 1.0 stays unset; any other gain is picture adjustment round(gain*50),
  -- on the Display the Frame's Position names, else the Display last seen on its bound Output;
  -- a Frame with neither loses its gain (stated cost); if two Frames name one Display the higher gain wins.
  -- Frames -> Cover (owner q1): corners as the opening, the quarter turn as the angle (F-3), revision + 1,
  -- trim (crop) and gain dropped; configuration_revision + 1 so every Player refetches.
  -- Readiness (owner q1 cost; F-1): UPDATE frames SET position_generation = NULL;  -- one statement, alone
  UPDATE node_calibration_trials SET state = 'expired' WHERE state = 'active';
  ALTER TABLE frames DROP COLUMN preview, DROP COLUMN preview_expires;
  ```
  The stored `frames.calibration` after 075 is exactly `{"revision": r + 1, "fit": {"mode": "cover", "corners": <corners>, "rotation": <0|90|180|270>}}` (the retiring fields take their defaults on read).
- `central/displays/picture.py`: C1 implements `default_picture`, `placed_at_first_probe`, `effective_picture` (no session yet: `stored`), `reverted`, `output_picture`.
- `central/infra/display_store.py`: `DisplayDocuments` projects each Output's document with `picture_change` and `picture` = `output_picture(effective_picture(stored, session, now))` of the Display last seen on the Output (`default_picture()` with no Display: unplaced, unset); `picture_change` starts at 1 and rises only when the picture part's digest (`picture_digest`, sha256 of the picture part's canonical encoding) differs, and `change` only when the rest differs; `OutputReportJudge` records `report.picture` (it is in `output_displays.report`) and, on the first report with a picture part for a Display whose settings are unplaced, applies `placed_at_first_probe` in the same transaction; a later report never re-places.
- The V1 calibration route is removed: `central/app.py`'s `CalibrationRequest` and `POST /v1/operator/frames/{id}/calibration`, `registry.calibrate`, `registry.calibration_mode_in`, `_expire_previews` and every `preview` read; `OutputBinding.preview` is written as None (the field leaves the contract in I1).
- Brightness is never a Position commit: nothing in Central reads or writes `Calibration.gain` after C1.
**Acceptance:**
- `tests/test_display_picture_settings.py` (DB): (1) a Display's settings follow it to another Pi's Output (serial-keyed); (2) first probe: a report answering 0x10 but not 0x12 places brightness on the display and contrast as picture adjustment; a later report answering both changes nothing (sticky); (3) a document's picture_change rises when brightness changes and `change` does not; a power test raises `change` and not `picture_change`; (4) an unplaced setting goes in the document unset; (5) migration: a Frame with gain 1.5 bound to a Display gives that Display picture-adjustment brightness 75; gain 1.0 leaves it unset and unplaced; (6) migration: a Frame with corners C and rotation 90 reads `CoverFit(corners=C, rotation=90)`, revision + 1, and is `position-needed`; (7) the V1 route answers 404/405.
- `tests/integration/test_central_display_picture.py` (DB + NATS, CI `db` leg): a brightness change stored on the Display reaches the Node's desired `output-HDMI-A-1` with the new picture_change within 3 s; a report with a picture part from the Node is recorded and places the settings.
- Existing tests that use `preview`, `calibrate` or `gain` move to the new shapes and stay green.
**Mutation probes:** raise `change` with the picture -> case 3 fails; re-place on every report -> case 2's sticky fails; write gain 1.0 as 50 % -> case 5 fails; drop the readiness statement -> case 6's `position-needed` fails (it is the F-1 line: the probe proves the statement is the only thing making Frames not ready).
**Land:** `feat(central): picture settings on the Display, in the Output document with their own change; Frames migrate to Cover (1c C1)`.

## C2: the picture session, Done, Revert and Copy (lane central)

**Owned:** see the lane table.
**Design:** design-draft "Live model on the Picture tab", `PictureSession`, `copyPicture`; design language §8 (Live model, B5); the parked note "the Picture session and Position's trial share a pattern: one shared module".
**Frozen page:** `central/live_session.py`, the session half of `central/displays/picture.py`, the views, ports and routes exactly as frozen in F0. Additions:
- `display_store`: `class PgPictureSessions` implementing `PictureQueries` and `PictureCommands` over `displays` and `picture_sessions` (every write `NOTIFY photo_wall_output_documents, '<device_id>'` in its transaction, as the power commands); the document source's `changed()` also returns at the earliest `idle_until` of a session on its Node, so an abandoned session's revert reaches the Pi without a reaper.
- `begin_picture` records `before_brightness` / `before_contrast` from the latest report's picture `read` values.
- `copy_picture` writes `copied(source, target)` for each target's Display; outcomes in request order.
- `central/fleet/node_calibration.py` uses `live_session.next_sequence` and `Lease`/`touched` for its sequence and idle checks (its error codes unchanged: `StaleSequence` maps to its existing code).
- `create_app` mounts `mount_picture_routes` with `PictureRefused` answered like `DisplayRefused`.
**Acceptance:**
- `tests/test_display_picture_settings.py` (DB), continued: (8) an edit with the wrong expected sequence is `stale_sequence` and changes nothing; (9) Done before the report answers the latest picture change is `not_presented_yet`, after it stores the draft; (10) Revert restores the stored values, and a brightness that was unset and edited goes to the display's read before the session; (11) a session left alone past its idle deadline: the document shows the reverted values with no request made; the next `begin_picture` stores them; (12) Copy from Frame A to B and C copies the four values, never `acts_on`, never B's or C's `calibration`, `profile` or `position_generation`; a target fed by A's own Display is `same-display`; one with no Display is `no-display`; (13) an edit or Done never changes the Frame's `calibration` revision, `configuration_revision` or readiness (brightness is no Position commit); (14) placing onto the display when the report says it did not answer is `display_did_not_answer`.
- `tests/test_live_session.py` (unit): lease slides but never past hard; ended lease refused; `next_sequence` refuses a stale one.
- `tests/test_node_calibration.py` stays green on the shared rules.
**Mutation probes:** overwrite the whole record on edit -> (8) fails; Done without the report check -> (9) fails; Copy carrying `acts_on` -> (12) fails; a reaper-free projection that ignores `idle_until` -> (11) fails; edit raising the position revision -> (13) fails.
**Land:** `feat(central): the Picture tab's live session, Done, Revert and Copy (1c C2)`.

## C3: Frame fit through trial and Save (lane central)

**Owned:** see the lane table.
**Design:** design-draft "Frame fit and Cover" (Frame profile, Orientation rows); F-4.
**Frozen page:**
- Trial drafts and Save carry `Calibration` with `fit` (the console sends `{"revision": r, "fit": {...}}`); `NodeCalibration` validates through `Calibration`, as today.
- `cover_profile` as its docstring. `registry.commit_calibration_trial_in`, when the committed `fit` is a `CoverFit`: reads the bound Output's size from `outputs.observation` (`width_px`, `height_px`; 0 or missing -> `RegistryError("output_size_unknown", 409)`), computes `cover_profile`, refuses `RegistryError("fit_orientation_mismatch", 409)` unless `_orientation_coherent(width_mm, height_mm, profile.width_px, profile.height_px)`, and writes `frames.profile` in the same statement as the calibration, without raising `generation` (the commit's own `commit_position` stamps the current generation). In Corners the profile is not touched (set as today).
- The Player configuration's `OutputBinding.calibration` carries the fit (no new field).
**Acceptance:** `tests/test_frame_fit_settings.py` (DB): (1) a Cover draft at 7 degrees through trial and Save reaches `configuration_for(...)` with the fit, and the Frame is ready; (2) Save in Cover writes the profile shape (1920x1080 output, a centred 960x540 opening at 0 degrees -> 960x540), and the Frame stays ready (no generation rise); (3) a landscape Frame saved at 60 degrees is `fit_orientation_mismatch`; at 90 degrees on a portrait-mm Frame it saves; (4) Corners with a skewed quad saves and leaves the profile as it was; (5) a folded quad in either mode is refused before any write; (6) an Output that reported no size refuses Cover Save with `output_size_unknown`.
**Mutation probes:** raise the generation when writing the profile -> (2)'s ready fails; skip the orientation check -> (3) fails; write the profile in Corners -> (4) fails.
**Land:** `feat(central): Cover and Corners through Position's trial and Save; Save writes the Cover profile (1c C3)`.

## K1: the Picture tab (lane console)

**Owned:** see the lane table.
**Design:** `.claude/skills/console-ux/SKILL.md` (procedure); design language §8 row B5 (Live model); design-draft "Live model on the Picture tab", "What the tab and the base page say"; operator-console-design §5 Picture, catalogue #111-#114, #119.
**Frozen page:**
- `pictureApi.js` (JSDoc types from `central/displays/views.py`, each returning `{ok: true, …} | {ok: false, error}` through `apiWrite` as `powerApi.js`): `readFramePicture(frameId)`, `beginPicture(frameId)`, `editPicture(sessionId, {setting, value, expected_sequence})`, `placePicture(sessionId, {setting, acts_on, expected_sequence})`, `keepPictureAlive(sessionId)`, `pictureDone(sessionId)`, `revertPicture(sessionId)`, `copyPicture(frameId, toFrames)`.
- `pictureSession.js`: `export function usePictureSession({frameId, open}): {view, phase: "idle"|"live"|"waiting"|"shown", change(setting, value), place(setting, actsOn), done(): Promise<boolean>, revert(): Promise<void>, busy, error}`: begins on open, keepalive at the cadence `liveAdjustment.js` uses, sends at most 2 edits per second while dragging and the last on release, ends (Revert) when the tab closes.
- `domain/frame-picture.tsx`: `export function FramePicture(props: {frameId: string, softwareHref: string | null}): JSX.Element`. Four sliders: Brightness and Contrast 0-100 %, Colour temperature 3000-9300 K step 100, Gamma 1.8-2.6 step 0.1; each with its where-it-acts label: "On the display · 60 %", "Photo Wall picture adjustment · 40 %", "As the display has it", "The display did not take it", "Changed on the display's buttons: 35 %", "Applies when the display is connected"; a control to move brightness or contrast to picture adjustment (and back when the display answered); Done (enabled at `shown`), Revert; Copy to other Frames (a Frame picker; says it copies the four values, never position). Never the words Panel, Actuator, Display Host, calibration, gain.
- `pages/frame-page.tsx`: Position and Picture stop sharing one adjustment: `useFrameAdjustment` serves Position only; the LeaveGuard covers each tab's own session.
**Acceptance:** `tests/browser/test_frame_picture_browser.py` (CI `browser`; a fake Central for the Pi's reports, the real Registry and display store): the label says "On the display" for a placed-on-display brightness and "Photo Wall picture adjustment" otherwise; dragging posts at most 2 edits per second and the last on release; Done stays disabled until the report answers the latest change, then keeps it; Revert puts back the stored value; moving brightness to picture adjustment changes the label; Copy to two Frames copies the display values and leaves both Frames' position unchanged (their corners, fit and readiness read back the same). The old brightness case in `test_console_frame_page_browser.py` moves here. Typecheck, lint, the catalog leg for the new stories.
**Mutation probes:** enable Done before `shown` -> the Done case fails; send every drag event -> the rate case fails; Copy sending `acts_on` or the fit -> the Copy case fails.
**Land:** `feat(console): the Picture tab: brightness, contrast, colour temperature and gamma, live, with Copy (1c K1)`.

## K2: the Position tab's fit (lane console)

**Owned:** see the lane table.
**Design:** owner q1 and the angle answer; design-draft "Frame fit and Cover"; operator-console-design §5 Position, catalogue #96-#99.
**Frozen page:**
- `domain/frame-position.tsx`: `FramePosition` keeps its props; adds a Fit choice, **Cover** (selected for every Frame whose `fit.mode` is cover) and **Corners** ("Stretch the picture exactly onto the corners", an explicit opt-in); Rotation: buttons 0, 90, 180, 270 and a fine control (a number field and a slider, 0-359.9, step 0.1) in Cover, the four buttons only in Corners. "Trim edges" and every `crop` control are gone; Reset to full screen keeps the fit and sets the angle to 0.
- `useDraft.js`: the draft is `{revision, fit}`; `draftKey` covers `fit` only; `CROP_REASON` and `cropValid` are deleted; `convex.js` `isConvex` is checked against `tests/support/frame_fit_vectors.json` (T0's shared vectors) by `tests/test_console_convex.py` (unit leg).
- `patterns/quad-editor.tsx`: draws the four corners only (no crop rectangle).
- `pages/frame-page.tsx` `outputSize`: no longer swaps by `calibration.rotation`.
**Acceptance:** `tests/browser/test_frame_fit_browser.py` (CI `browser`): a Frame opens on Cover; choosing Corners sends a `CornersFit` draft and back; the 90 button and a typed 7.5 each send the angle; Trim edges is absent; Save in Cover then reads the profile shape back on the Hardware tab; a 60-degree Save on a landscape Frame shows the `fit_orientation_mismatch` message in plain words. `test_console_frame_page_browser.py`'s corner drag, nudge and folded-quad cases pass on the new draft.
**Mutation probes:** default new drafts to Corners -> the opens-on-Cover case fails; keep a crop field in the draft -> the Trim-absent and draft-shape cases fail.
**Land:** `feat(console): Position chooses Cover or Corners with any rotation; Trim edges removed (1c K2)`.

## X1: docs (lane docs)

**Owned:** `docs/operator-console-design.md` (rows #96-#99, #111-#114, #119; §5 Position and Picture; §6 storage split), `docs/execution-contract.md` (picture settings are Display state, outside a Frame's content), `docs/display-host-backend.md` and `docs/player-architecture.md` and `docs/player-node-domain-model.md` (the Player is the only picture pipeline; Display Host carries settings and DDC/CI; frame protocol v4; "Panel brightness unavailable" replaced), `docs/architecture.md` (renderer order with the masks; Cover), `docs/roadmap.md` (1c status, acceptance corrected per F-5, the test names landed), `docs/design-language.md` (row B5 if the words changed), `docs/requirements.md` (only where an owner answer changed a written rule: Trim gone, Cover default; say so), `AGENTS.md` (code map: `central/displays/picture.py`, `central/live_session.py`, `contracts/frame_fit.py`, `appliance/display_host` picture), `.claude/skills/display-control/SKILL.md` (VCP 10/12 through the per-port channel; newest wins; the bench recipe). Every rule in one home; others link. `python3 scripts/check_docs.py` green.
**Land:** `docs: picture settings on the Display and the Cover fit (1c X1)`.

## I1: integrate (lane integrate, last)

**Owned:** every file naming a retiring shape (the deletion's doc scope is every file naming the deleted symbol), `tests/native_picture_e2e_harness.py`, `scripts/run_display_harness.py`, `scripts/release_plan.py`.
**Frozen page (the tripwire):**
- `contracts/models.py`: `Calibration` loses `rotation`, `corners`, `crop`, `gain`, the retiring check and the serializer; `fit: FrameFit = CoverFit()` (required in effect, Cover default, owner q1). `OutputBinding.preview`, `preview_expires`, `effective_calibration` are deleted (callers read `calibration`).
- `contracts/node_output.py`: `SCHEMA_MAJOR = 2`; `OutputDocument.picture_change: int` and `picture: OutputPicture` required (no defaults; the "both or neither" check goes); Central's link store refuses a schema-1 report (the display line's envelope major), so an old Pi's reports are not recorded (design cost, no backwards compatibility).
- `player/geometry.py`: `fit_of` becomes `calibration.fit` (inline it and delete `fit_of`), `cover_rect`, `source_uv`, `inverse_rotation`, `compose_pixel`'s `gain` deleted; `picture.PictureController.document` loses its "picture None" arm.
- Console: nothing reads `calibration.corners` / `rotation` / `crop` / `gain` (grep is empty).
**Acceptance:**
- `tests/native_picture_e2e_harness.py` (CI node-pid1 `display-harness`, about 40 s with R2's): real Weston with the real shell and the real frame client, the real `PictureController` with a fake `ddcutil` that never answers, a real `NativeRenderer` showing a grey Frame on a Cover fit: putting a document with brightness 30 as picture adjustment and contrast 25 as picture adjustment changes the captured pixels (`weston_capture_v1`) inside the opening to the reference values, leaves every pixel outside the opening black, and the controller's report reaches `presented_change` equal to the document's picture change.
- The whole unit, DB, node-bus and browser tiers green in CI with the old shapes gone.
**Mutation probes:** keep `picture` optional -> a schema-1 style document still decodes (add the case "a document without a picture part is refused"); drop the shell's `presented` echo -> the e2e `presented_change` assertion times out.
**Land:** `refactor(display): retire the pre-1c Position and Output document shapes; end-to-end picture test (1c I1)`.

## Outcome (filled by the run's closing step)

| Slice | Outcome | sha |
|---|---|---|
| F0 | landed | (this commit) |
