# Run ledger: roadmap 1b, Display identity and power

**Branch:** `feat/display-identity-power` (from `origin/main` at d5dd23f), ONE pull request (owner, 2026-10-10).
**Rules:** `~/.claude/skills/implementation-workflow/SKILL.md` (binding). An implementer reads **its slice page below, the owner answers, and the design sections the page names**, nothing else.
**Design (accepted):** `/Volumes/Dock/tmp/display-1b/revised.md` (sections above "=== PARKED ===" were shown to the owner; PARKED is for code architects). Spike: `/Volumes/Dock/tmp/display-1b/spike.md`. Code facts: `facts.md`. Design review: `review.md`. Crash evidence: `pi-crash-evidence.log`, `pi-journal-boot.log.gz` (same folder). Gate artifact: https://claude.ai/artifact/DjZTF9HGKWLVy6kexikeUp
**Errata:** `grep -a 'E-1B-' .claude/errata.md`. A page changes only through an erratum and a re-cut: an implementer who hits a wall appends `E-1B-<slice>-<n>` and STOPS.

## The problem in plain words

The owner cannot turn a display off or on from the console, and Photo Wall does not know which display is plugged into which Pi port. 1b makes the Pi read each display's identity from its EDID and report it over the Node bus; Central recognises the same display when it moves (by serial, or by make and model tied to its Frame when it has no usable serial), keeps a Frame not ready when its display changed until its Position is re-checked, and writes what power it wants per port as a document the Pi carries out by itself (CEC, DDC/CI, or stopping the HDMI signal). A Test on the Frame's new Power tab turns the display off for 5 minutes, or less if Turn on is pressed. The image gains the DDC/CI driver and the two tools. In the same pull request the Player stops dying when its screen blips, a dead Player is restarted, Central notices a silent Player, and the "too many open files" leak is root-caused and fixed.

## Owner answers (binding; copied verbatim from `/Volumes/Dock/tmp/display-1b/answers.md`)

> # 1b owner answers (gate artifact https://claude.ai/artifact/DjZTF9HGKWLVy6kexikeUp)
>
> Answers are current choices (steers), not requirements.
>
> - q1, 2026-10-10 (artifact): "Test: turn off" lasts 5 minutes, or less if Turn on is pressed. When it ends, the request underneath takes over. The Pi counts the time itself.
> - q2, 2026-10-10 (artifact): a display with no usable serial is recognised by make and model, tied to the Frame it feeds (stays the same Display through a Pi swap or port change; moved to another Frame it starts over). Owner note, verbatim: "There might be duplicate make/model attached to both pi HDMI’s". => two identical no-serial monitors on one Pi's two ports must stay two Displays (each tied to its own Frame); an unbound port holds its display pending until bound. Needs a CI test.
> - q3, 2026-10-10 (chat): the Player crash fix goes INSIDE 1b's pull request (not a separate PR). Scope: the Player survives its HDMI output going away and coming back (signal blip), the Pi restarts a Player that dies, Central notices a silent Player, and the "too many open files" (EMFILE) fault is root-caused and fixed.
> - Earlier (2026-10-10, chat): 1b ships as ONE pull request; the roadmap's three-way split is only the internal build order (image tools -> Display identity with G5 -> power methods + Power tab).
>
> Design accepted: revised.md in this folder (sections above "=== PARKED ===" were shown to the owner; PARKED notes are for code architects). Spike: spike.md. Code fact sheet: facts.md. Design review: review.md.
> Crash evidence from the test Pi (before reboot): pi-crash-evidence.log, pi-journal-boot.log.gz (full boot journal, 48k lines; Player SEGV at 2026-10-10T16:06:37Z right after a DDC standby HPD bounce; ~1,000 EMFILE faults since 2026-10-09 23:34).

## Lanes, slices and order

Build order (owner): image tools, then Display identity with G5, then power methods and the Power tab. Tracer first: **T1** is the thinnest end-to-end path through the riskiest seam, Central's first production desired document crossing the Node bus to the display line and the report coming back.

| Lane | Slice | What it delivers | Owned paths (outputs its checks write included) | Depends on | Runs concurrently with |
|---|---|---|---|---|---|
| bus | **T1 (tracer)** | Output document and report codec; NodeLink asserts on `changed()`; round trip on a real bus | `contracts/node_output.py`, `nodeapi/hub.py`, `tests/test_node_output_contract.py`, `tests/integration/test_node_bus_display_line.py`, `tests/integration/bus_servers.py` | F0 | I1 |
| pi-image | **I1** | i2c-dev in the initramfs and loaded at boot; ddcutil and v4l-utils in the image; image checks | `appliance/netboot_initramfs/hooks/photo-wall-netboot`, `scripts/verify_netboot_initrd.py`, `tests/test_verify_netboot_initrd.py`, `appliance/rpi_image_gen/layer/photo-wall-os.yaml`, `scripts/device_root_checks.py`, `tests/test_device_root_checks.py`, `debian/photo-wall-node.sysusers` (only if E-1B-I1 finds the `i2c` group missing), `.github/workflows/base-image.yml` (only if its required list does not derive from the layer) | F0 | T1, D1, C1, P1 |
| pi-display | **D1** | The display bus session; Output report with EDID identity and modes, per Output | `appliance/display_host/bus.py`, `appliance/display_host/edid.py`, `appliance/display_host/runner.py`, `tests/node/display/test_display_output_report.py`, `tests/node/display/test_node_display_runner.py` | T1 | I1, C1, P1 |
| pi-display | **D2** | Power methods, the Pi's controller and timer, signal off in the Weston shell, device access | `appliance/display_host/output_power.py`, `appliance/display_host/power_methods.py`, `appliance/display_host/weston.py`, `appliance/display_host/native/shell.c`, `appliance/display_host/runner.py`, `appliance/systemd/photo-wall-display-controller.service`, `tests/node/display/test_display_power_methods.py`, `tests/node/display/test_node_display_runner.py`, `tests/node/health/test_judge_output_power.py`, `tests/test_node_display_native.py`, `appliance/kernel/capacity.py` and `tests/node/test_node_memory_class.py` (only if MemoryMax changes) | D1 | C2, C3, K1, P1 |
| central | **C1** | Displays, identity matching, last seen per Output, the Output report judge, the Output document source, GET display | `central/migrations/072_displays.sql`, `central/displays/`, `central/infra/display_store.py`, `central/infra/node_link_store.py`, `central/infra/node_links.py`, `central/node_bus_wiring.py`, `central/display_routes.py`, `central/app.py`, `central/registry.py`, `tests/test_display_identity.py`, `tests/integration/test_central_display_documents.py`, `tests/integration/test_central_node_links.py` | T1 | I1, D1, P1 |
| central | **C2** | Readiness worked out from Position commits; `frames.calibration_valid` replaced outright; display-changed; profile editable while changed | `central/migrations/073_position_display.sql`, `central/displays/`, `central/infra/display_store.py`, `central/registry.py`, `central/installation_models.py`, `central/installation_repository.py`, `central/readiness_diagnostics.py`, `central/app.py`, `central/console/src/unfinished.js`, `central/console/src/health.js`, `central/console/src/pages/frame-page.tsx`, `central/console/src/domain/frame-profile.tsx`, `tests/test_display_changed_readiness.py`, and every test file naming `calibration_valid` (today: `tests/test_registry.py`, `tests/test_console_interruption.py`, `tests/test_console_wall_facets.py`, `tests/test_console_update_wall.py`, `tests/test_operator_frames.py`, `tests/test_node_lifecycle.py`, `tests/test_console_wall_layout.py`, `tests/browser/test_operator_frame_profile_browser.py`, `tests/browser/test_operator_showrunner_browser.py`) | C1 | D2, P1 |
| central | **C3** | Power requests, Test, power settings, GET power; the worker woken from the API | `central/migrations/074_power_requests.sql`, `central/displays/`, `central/infra/display_store.py`, `central/display_routes.py`, `central/app.py`, `central/node_bus_wiring.py`, `tests/test_display_power_requests.py`, `tests/integration/test_central_display_power.py` | C2 | D2, K1, P1 |
| central | **C4** (test-first) | Central notices a silent Player (owner q3): the incident replayed against Central; a red result stops with an erratum (E-1B-15) | `tests/test_central_silent_player.py`, `tests/browser/test_silent_player_browser.py`; production files only through a re-cut | C3 (lane order), K2 (both build `central/console/dist/`) | P1a, P1b, D2 |
| player-reliability | **P1a** | The Player's decoders release their pipelines (the EMFILE leak); a surface failure ends with its episode (the blip); EMFILE is a local cause, not a network one | `player/native.py`, `player/output_discovery.py`, `tests/test_player_service.py`, `tests/native_player_resource_harness.py`, `scripts/run_display_harness.py`, `scripts/release_plan.py`, `tests/test_release_plan.py` (only if it pins the suite's paths), `uplink/causes.py`, `tests/test_uplink_causes.py` | F0, F0b | every slice, P1b included |
| player-reliability | **P1b** | The broker restarts a dead Player on a backoff; the judge raises `app_absent` and `app_resource_exhausted`; Python tracebacks on a crash | `appliance/apps/broker.py`, `appliance/apps/broker_runner.py`, `appliance/apps/online_broker.py`, `appliance/apps/online_runner.py`, `appliance/apps/process_linux.py`, `appliance/apps/lifecycle_storage.py`, `appliance/apps/descriptors.py`, `appliance/health/judge.py`, `scripts/node_control_demo.py`, `tests/node/test_node_control_m1.py`, `tests/node/apps/test_node_online_broker.py`, `tests/node/apps/test_node_probe_kill.py`, `tests/node/test_node_probe_broker.py`, `tests/test_node_switch_convergence.py`, `tests/node/test_node_linux_adapters.py`, `tests/node/test_health_judge.py`, `tests/node/health/test_app_absent.py`, `tests/node/apps/test_app_descriptors.py`, `tests/node/apps/test_broker_app_facts.py`, `tests/test_node_pid1.py` (+ `tests/node_pid1_central_inner.py` only if the scenario step needs it) | F0, F0b | every slice, P1a included |
| console | **K1** | Hardware tab: Display and the display-changed card | `central/console/src/domain/frame-display.tsx`, `central/console/src/domain/frame-display.stories.tsx`, `central/console/src/displayApi.js`, `central/console/src/pages/frame-page.tsx`, `tests/browser/test_frame_display_changed_browser.py`, `central/console/dist/` (its build) | C2 | D2, C3, P1 |
| console | **K2** | Power tab: method, switches, Test off/on | `central/console/src/domain/frame-power.tsx`, `central/console/src/domain/frame-power.stories.tsx`, `central/console/src/powerApi.js`, `central/console/src/pages/frame-page.tsx`, `central/console/src/routes.js`, `tests/test_console_routes_r4.py`, `tests/browser/test_frame_power_browser.py`, `tests/browser/catalog-baselines/` (new stories' baselines), `central/console/dist/` | C3, K1 | C4, P1 |
| docs | **X1** | Design homes updated to what landed; the display-control skill; the Player's restart and fault rows | `docs/operator-console-design.md`, `docs/design-language.md`, `docs/execution-contract.md`, `docs/requirements.md` (only where an owner answer changed a written rule), `docs/roadmap.md`, `docs/runbook.md`, `docs/player-architecture.md` (the restart rule, `app_absent`, `app_resource_exhausted`), `AGENTS.md`, `.claude/skills/display-control/SKILL.md` | drafts from the pages at once; final pass after K2, D2, C4, P1a, P1b | everything |

**Fan-out.** T1, I1, P1a and P1b start together (P1a and P1b are disjoint: `player` + `uplink` against `appliance/apps` + `appliance/health`; P1 imports nothing T1 changes). After T1 lands: D1 and C1 fan out (`appliance/display_host`, `central` + `nodeapi`-free), I1 continues. C4 runs after C3 and K2 (the browser tests of both build `central/console/dist/`). Inside a lane, slices run in order (D1 then D2; C1 then C2 then C3). K1 waits for C2 (it reads `readiness`, and C2 edits `frame-page.tsx`), K2 for C3 and K1. Only the central lane adds migrations and touches `central/` Python; only T1 touches `contracts/` after F0. `central/console/` belongs to the console lane except the four files C2 migrates off `calibration_valid` in the same compile unit (co-change, skill §3.2): C2 lands before K1, so they never overlap.

**Seams with player-reliability (P1a, P1b).** (a) The blip's root cause is not in D2's files: the shell revoking the grant and unmapping the app's surface on a disconnect (`native/shell.c` `invalidate`) is correct; the SEGV is Mesa's software EGL failing to allocate a buffer under EMFILE. P1 changes no Weston or shell file; if P1a's harness shows otherwise, P1a appends an erratum and the fix becomes a P1c sequenced after D2, never a parallel edit. (b) "Central notices a silent Player" is Central code: C4, test-first (E-1B-15). (c) `appliance/health/judge.py` is P1b's; D2's `test_judge_output_power.py` only reads it, and P1b's new codes fire only on the broker's facts (`app_exited`, `app_descriptors`), never on display facts, so D2's guard stays green. (d) Contracts: the two fault rows are frozen in F0b (`contracts/node_faults.py`); P1 adds and changes no contract; P1a points `player/output_discovery.py` at `contracts.node_output.OUTPUT_IDS` (rule of two) and deletes `CONFIGURED_OUTPUT_IDS`. (e) "A blip is not a display change" (C1, D2) and "the Player survives a blip" (P1a) are separate tests of separate behaviours. (f) `appliance/kernel/app_facts.py` (F0b) is the apps-to-health fact vocabulary; no slice changes it. (g) systemd-coredump goes in I1's `photo-wall-os.yaml`: a residual bead after I1 lands, never a parallel edit (E-1B-14).

**Land command (every slice).** From the lane's worktree, with `<owned>` = the slice's owned paths that exist:
```
git add -A -- <owned> && git commit -m '<msg>' -- <owned>
git pull --rebase origin feat/display-identity-power && git push origin HEAD:feat/display-identity-power
```
A commit that meets a sibling's `index.lock` waits and retries. After a lane's last slice, `git status --porcelain` is empty; anything left is a write outside an owned list and stops the run. Slice agents do not edit this ledger; the run's closing step fills the outcome table.

**Bench step (NOT RUN in the PR).** An agent runs Test off and on against the test Pi's monitor and reads the result back, and checks that the monitor stays in DDC/CI standby after Weston re-enables the Output (the design's open bench question). It needs a deployed release, so it runs after merge and deploy (iac PR) and is listed as NOT RUN in the PR body until then.

## Environment and gates

- Checkout: each lane's own worktree (`isolation: "worktree"`); `.venv/bin/python` after `UV_CACHE_DIR=/Volumes/Dock/tmp/uv-cache uv sync --frozen`; revert `uv.lock` if anything rewrites it. No slice changes a dependency.
- Console: `PATH=$HOME/.nvm/versions/node/v22.23.2/bin:$PATH` before any test or npm run (the console tests fail under the shell's default Node 16).
- pytest `--basetemp=/tmp/pw1b/<slice>/<gate>` (short: AF_UNIX socket paths; a `/Volumes/Dock/tmp/...` basetemp fails 5 socket tests with "AF_UNIX path too long").
- `PHOTO_WALL_NATS_SERVER=/Volumes/Dock/tmp/nats/nats-server-v2.15.0-darwin-arm64/nats-server`.
- DB tier: `docker compose -f tests/integration/compose.test-database.yml up -d --wait`, then `.venv/bin/python scripts/test_local.py -q -m db -n 4 --dist loadgroup`.
- New integration tests that need the database are named `tests/integration/test_central_*.py` (DB tier); bus-only ones `tests/integration/test_node_bus_*.py` (node-bus tier).
- Locally each slice runs its changed packages' tests, ruff, lint-imports, check_docs, and the console typecheck and lint when it touches the console; CI (`pipeline.yml`) is the full gate.

| Gate | Command |
|---|---|
| static | `.venv/bin/python -m ruff check .` · `.venv/bin/lint-imports` · `python3 scripts/check_docs.py` |
| unit | `PHOTO_WALL_TEST_REQUIRE_DATABASE=1 .venv/bin/python -m pytest -q -m "not db and not browser" -n 4 --dist worksteal <paths>` |
| bus | `PHOTO_WALL_NATS_SERVER=… .venv/bin/python -m pytest -q tests/integration -k node_bus -n 4` |
| db | as above, `-m db <paths>` |
| browser | `PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q tests/browser/<file> -n 4 --browser chromium` (built console) |
| console | `npm --prefix central/console run typecheck` and `run lint`; `npm --prefix central/console run build` |

**Preflight at F0 (2026-10-10, this branch after the freeze):** ruff 0; lint-imports 21 kept, 0 broken; check_docs 0; unit tier 3751 passed, 130 skipped, 5 failed only under a long basetemp (AF_UNIX path; the same 62 tests pass with `/tmp`), 31 more only with the default Node 16. Node-bus, DB, browser tiers: NOT RUN at F0 (F0 changes no behaviour: protocol declarations and NotImplementedError stubs). The Debian build's import check (`unreached-module`): NOT RUN locally; F0 makes `appliance.display_host.runner` import `bus`, which imports `contracts.node_output`, so the new shared module is reached.

**Budget gates.** Per slice: 90 min wall-clock (120 for T1, C1, C2, D2), at most 8 agents. Feature: 12 h wall-clock and 8 M tokens; warning at 80 %, stop after the current slice at 100 %. At most two fix cycles per gate, then a pushed `wip/1b-<slice>` branch and stop. A course-correction architect pass after the fifth implementer (owner memory) and before the end review. Models pinned by id at launch: `claude-opus-5-5` for T1, D1, D2, C1, C2, C3, K1, K2, P1a, P1b, C4; `claude-sonnet-5-5` allowed for I1 and X1 (owner, 2026-10-09).

**End (once, over BASE..HEAD):** CI watcher on the PR; verifier (each page's mutation probes, conformance to the pages); review lenses in parallel with a **security lens** (migrations, a public operator API, device access in a systemd unit) and a domain lens on the identity and readiness rules; one fix agent. Then the PR is marked ready and mcurcio is requested, with before/after screenshots (dark and light) of the Hardware and Power tabs in the body.

---

## F0: the freeze (this commit; code architect)

Interfaces as compiling code, bodies `NotImplementedError` except what must run at import:

| File | What is frozen |
|---|---|
| `contracts/node_output.py` | The wire: `PowerMethod`, `Power`, `RequestReason`, `PowerResult`, `BEST_DETECTED`, `METHOD_PRECEDENCE`, `PowerRequest`, `OutputDocument`, `DisplayIdentity`, `DisplayMode`, `InForce`, `OutputReport`, `PowerAttempt`, the six codec functions, `OUTPUT_IDS`, the size and count limits, `SCHEMA_MAJOR`; `output_document_key` and `output_report_key` are real (`"output-<output_id>"`) |
| `appliance/display_host/bus.py` | The bus placement, real and checked at import: `DISPLAY_SLICE` on the existing `display` line (records 1,512,297 B + state 42,881 B + desired 17,686 B = the line's 1,572,864 B; 3 streams; the store stays at 17 of 17); `DISPLAY_STATE`, `DISPLAY_DESIRED`, `POWER_RECORD`; `display_session` stub |
| `appliance/display_host/edid.py` | `parse_edid`, `read_edid` |
| `appliance/display_host/output_power.py` | The Pi-side ports and pure core: `PowerMethodAdapter` (probe, read, set, showing_other_input, claim_input; every call bounded by `timeout`), `ReportSink`, `Clock`, `Trigger`, `Decision`, `in_force`, `next_deadline`, `OutputPowerController` |
| `appliance/display_host/power_methods.py` | `DdcCiAdapter`, `HdmiCecAdapter`, `SignalOffAdapter`, `OutputPowerControl` (the compositor port), `Runner` |
| `nodeapi/hub.py` | `DocumentSource.changed()` |
| `central/infra/node_links.py` | `DocumentSources.source(serial, pipe)` |
| `central/infra/node_link_store.py` | `RecordJudge.judge_in(conn, device_id, stream, items)` |
| `central/displays/model.py` | `Readiness`, `PowerStatus`, `DisplayKey`, `Sighting`, `serial_usable`, `display_key`, `FramePosition`, `readiness`, `adopts`, `DisplaySettings`, `DEFAULT_SETTINGS`, `PowerTest`, `ProjectedOutput`, `project_output`, `power_status`, `TEST_SECONDS`, `standing_on` |
| `central/displays/views.py` | The operator API models: `FrameDisplayView`, `DisplayView`, `ModeView`, `FramePowerView`, `InForceView`, `PowerTestView`, `PowerTestRequest`, `PowerTestAccepted`, `DisplayPowerSettings` |
| `central/displays/ports.py` | `DisplayQueries`, `DisplayCommands`, `DisplayRefused` |
| `central/display_routes.py` | `mount_display_routes` and the four route declarations (not mounted until C1/C3) |
| `central/node_bus_wiring.py` | `_NoDocuments.changed()` (never returns) |
| `pyproject.toml` | `central.displays` joins the no-persistence domains and gets its own contract (no nodeapi, infra, db, registry, app) |
| `appliance/display_host/runner.py` | Imports `bus.display_session` (D1 starts it), so the build reaches `contracts.node_output` |

**F0b (the player-reliability freeze, its own commit; code architect).** Written from the root-cause report, `/Volumes/Dock/tmp/display-1b/player-root-cause/README.md`, with its repro scripts beside it (`leak.py`, `segv.py`, `gdb.sh`, `fd-samples.txt`). Bodies `NotImplementedError` except constants and the catalogue rows:

| File | What is frozen |
|---|---|
| `contracts/node_faults.py` | Rows `app_absent` (display-affecting, "Photos paused — the player stopped", raise 5,000 ms, hold 0) and `app_resource_exhausted` (degraded, "The player is running out of open files", raise 0, hold 60,000 ms); `tests/node/test_health_judge.py`'s catalogue test pins all four rows |
| `appliance/kernel/app_facts.py` | `APP_STARTED`, `APP_EXITED`, `APP_DESCRIPTORS`; `RunDocument`, `AppRunValue`, `AppDescriptorsValue` (real: constants and TypedDicts) |
| `appliance/apps/descriptors.py` | `DESCRIPTOR_SAMPLE_MS = 10_000`, `DescriptorUse`, `parse_soft_limit`, `descriptor_use` |
| `appliance/apps/broker.py` | `RELAUNCH_BACKOFF_MS = (1_000, 5_000, 30_000, 300_000)`, `STABLE_RUN_MS = 600_000`, `Launch`, `RelaunchPacing`, `RelaunchPacingStore`, `display_took_app`, `pace_relaunch`, `relaunch_absent` |
| `appliance/apps/lifecycle_storage.py` | `FileRelaunchPacing(store)` with `get`, `put` (key `relaunch-pacing`) |
| `appliance/health/judge.py` | `APP_ABSENT`, `APP_RESOURCE_EXHAUSTED`, `RESOURCE_PRESSURE_PERCENT = 80`, `descriptor_pressure` |

### Route table (the console lanes code against it)

| Method | Path | Body | Answer | Refusals | Mounted by |
|---|---|---|---|---|---|
| GET | `/v1/operator/frames/{frame_id}/display` | | `FrameDisplayView` | 404 `unknown_frame` | C1 (readiness field filled by C2) |
| GET | `/v1/operator/frames/{frame_id}/power` | | `FramePowerView` | 404 `unknown_frame` | C3 |
| POST | `/v1/operator/frames/{frame_id}/power-tests` | `PowerTestRequest {power}` | 202 `PowerTestAccepted {request_id, power, for_seconds}` | 404 `unknown_frame`, 409 `frame_unbound` | C3 |
| PUT | `/v1/operator/displays/{display_id}/power-settings` | `DisplayPowerSettings` | `DisplayPowerSettings` | 404 `unknown_display` | C3 |
| PUT | `/v1/operator/frames/{frame_id}/profile` (exists) | unchanged | unchanged | `frame_bound` no longer while the Frame is display-changed | C2 |
| GET | `/v1/operator/snapshot` (exists) | | each frame: `readiness` (`unbound`, `display-changed`, `position-needed`, `ready`) replaces `calibration_valid` | | C2 |

Every route takes the operator `admin` dependency. Refusals answer `{"error": code}` at the status, as `apiWrite.js` reads.

---

## T1 (tracer): the display line carries Central's first document (lane bus)

**Owned:** `contracts/node_output.py`, `nodeapi/hub.py`, `tests/test_node_output_contract.py`, `tests/integration/test_node_bus_display_line.py`, `tests/integration/bus_servers.py`.
**Design:** revised.md "Contracts", "Data flow", the `OutputDocument`/`OutputReport` block, PARKED "Desired documents are stubbed" and "Apply rule".
**Frozen page:**
- `contracts/node_output.py` bodies exactly as its docstrings say: validation in each `__post_init__` (ValueError with the named code), canonical JSON codec (`json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)`, enums as values, tuples as lists, `None` kept as `null`), decode through `contracts.strict_json.loads_object` with the size limits, unknown or missing fields refused. Field names on the wire are the dataclass field names. No change to any signature, constant or name in the file.
- `nodeapi/hub.py`: `NodeLink.run` additionally runs, while its drains run, a task that awaits `self._documents.changed()` and then calls `assert_documents(stream)` for every desired bucket of its pipe the last `reconcile` listed (kept as `self._desired: tuple[str, ...]`); a nats error or timeout in that assert is logged and the task waits for the next `changed()`; a store error propagates as from the drains. No other signature changes; `DocumentSource` keeps `documents` and `changed` exactly as frozen.
- `tests/integration/bus_servers.py`: `Projection` gains `changed()` backed by an `asyncio.Event` and a `set(stream, key, value)` that sets it.
**Acceptance (each red before, green after):**
- `tests/test_node_output_contract.py` (unit): every type round-trips; the encoding of a document is byte-identical across two encodes and across field-order permutations of the input; refusals: unknown output id, empty stack, a timed last request, duplicate request ids, `method` not in `answers`, `answers` out of `METHOD_PRECEDENCE` order, past `OUTPUT_DOCUMENT_BYTES`, duplicate JSON key, unknown field, NaN; `output_document_key("HDMI-A-1") == "output-HDMI-A-1"`, `ValueError` for `"HDMI-A-3"`.
- `tests/integration/test_node_bus_display_line.py` (node-bus tier, real nats-server and hub): (1) a `NodeSession("display", DISPLAY_SLICE, …)` applies the display line beside the host line on one bus and the store's stream count stays within `NODE_MAX_STREAMS`; (2) Central's `NodeLink` on the fleet pipe asserts `output-HDMI-A-1` from `Projection` and the session's `desired` view decodes it to the same `OutputDocument`; (3) `Projection.set(...)` with a new change reaches the Node's view within 3 s with no birth, no new stream and no reconnect (only `changed()` can deliver it); (4) the session puts an `OutputReport` under `output-HDMI-A-1` and the link's store records it on `KV_state_display`; (5) a document under a key the table does not list is not sent and `document_refused` is logged.
**Mutation probes:** remove the `changed()` task from `NodeLink.run` → (3) times out; drop `sort_keys` from the encoder → the byte-identity case fails; drop the "untimed last request" check → its refusal case fails.
**Land:** `feat(contracts): output document and report codec; NodeLink asserts on a projection change (1b T1)`.

## I1: image tools (lane pi-image)

**Owned:** `appliance/netboot_initramfs/hooks/photo-wall-netboot`, `scripts/verify_netboot_initrd.py`, `tests/test_verify_netboot_initrd.py`, `appliance/rpi_image_gen/layer/photo-wall-os.yaml`, `scripts/device_root_checks.py`, `tests/test_device_root_checks.py`; `debian/photo-wall-node.sysusers` and `.github/workflows/base-image.yml` only under the conditions in the lane table (state which in the commit body).
**Design:** spike.md "What the image needs"; PARKED "Pi side › Image".
**Frozen page:**
- The hook's display module loop becomes `for module in vc4 v3d i2c-dev; do` (the module tree is pruned and has no kmod binary; modules travel only in the initrd).
- `scripts/verify_netboot_initrd.py`: `DDC_MODULES: tuple[str, ...] = ("i2c-dev",)`, required beside `DISPLAY_MODULES` with the same refusal shape.
- `photo-wall-os.yaml`: `packages:` gains `ddcutil` and `v4l-utils` (cec-ctl; pulls libv4l2rds0t64; ddcutil pulls i2c-tools, libi2c0, usbutils); a `customize-hooks` line writes `/etc/modules-load.d/photo-wall-ddc.conf` containing `i2c-dev`; the layer's header comment says why (spike, 2026-10-10). No device-tree overlay (`vc4-kms-v3d-pi5` already makes i2c-13/14 and the CEC devices).
- `scripts/device_root_checks.py`: checks `/usr/bin/ddcutil`, `/usr/bin/cec-ctl`, the modules-load file's content, and the `i2c` group (`getent group i2c`, created by i2c-tools); each failure a named refusal like the existing checks.
**Acceptance:** `tests/test_verify_netboot_initrd.py`: an initrd without `i2c-dev` is refused, with it accepted. `tests/test_device_root_checks.py`: a root missing each of ddcutil, cec-ctl, the modules-load file and the group is refused by name; a complete root passes. CI's base-image leg runs the checks on the built image.
**Mutation probes:** remove `i2c-dev` from `DDC_MODULES` → the initrd refusal case fails; delete the cec-ctl check → its refusal case fails.
**Land:** `feat(image): DDC/CI driver loaded at boot, ddcutil and cec-ctl in the base (1b I1)`.

## D1: the Output report (lane pi-display)

**Owned:** `appliance/display_host/bus.py`, `appliance/display_host/edid.py`, `appliance/display_host/runner.py`, `tests/node/display/test_display_output_report.py`, `tests/node/display/test_node_display_runner.py`.
**Design:** revised.md rule 1, `OutputReport`, `DisplayIdentity`; spike "Display identity"; PARKED "No display bus session exists".
**Frozen page:**
- `bus.display_session(release_digest, *, url)` as its docstring; `release_digest` is `DISPLAY_SLICE.digest`.
- New in `bus.py`: `class BusReportSink:` implementing `output_power.ReportSink`: `__init__(self, session: NodeSession) -> None`; `put_report(report)` = `session.state.put(output_report_key(report.output_id), encode_output_report(report))`; `emit_attempt(attempt)` = `session.events.emit(POWER_RECORD, encode_power_attempt(attempt), schema_major=SCHEMA_MAJOR)`.
- `edid.parse_edid`, `edid.read_edid` as their docstrings (serial rule in `DisplayIdentity`'s docstring; product little-endian at bytes 10-11; maker the 5-bit packed PNP letters at bytes 8-9).
- `runner.main()` starts the session (always, with or without the retiring Central config) and, for every Output the backend reports (`controller.host.states()` after each `observe`), puts an `OutputReport` with `connected`, `identity`/`modes` from `read_edid` + `parse_edid` when connected (None and `()` when not), and `answers=()`, `method=None`, `for_change=None`, `result=None`, `in_force=None` (D2 fills those). A report is put only when it differs from the Output's last.
**Acceptance:** `tests/node/display/test_display_output_report.py` (unit; EDIDs built by a helper in the test): the spike's monitor (XYM, 5475, "MNN", serial 0, blank serial text) → `DisplayIdentity("XYM", 5475, "MNN", None)` and its 1920x1080@60 preferred mode; a numeric serial → its decimal; a serial-text descriptor wins over the number; a bad checksum → identity None; a disconnect → `connected=False, identity=None`; a reconnect with the same EDID → the same identity; an unchanged Output puts nothing.
**Mutation probes:** read the product big-endian → the 5475 case fails; drop the "differs from last" check → the no-put case fails.
**Land:** `feat(display): the display line's session reports each Output's EDID identity (1b D1)`.

## D2: power on the Pi (lane pi-display)

**Owned:** see the lane table.
**Design:** revised.md rule 2, "Contracts" rows "Pi display controller" and "Power methods", the support table; PARKED "Pi side" (Unit, Signal off, Adapters, Apply rule, Request end, Health); spike runbook. Owner q1.
**Frozen page:** `output_power.py` and `power_methods.py` exactly as frozen in F0, their docstrings being the contract. Additions:
- `weston.WestonBackend.output_power(self, output_id: str, power: Power, *, timeout: float) -> bool` and `output_powered(self, output_id: str) -> Power | None` (implements `power_methods.OutputPowerControl`), sending the control-socket op `{"op": "output_power", "output_id": <id>, "power": "on"|"off"}` and waiting for the shell's `{"event": "output_power", "output_id", "power"}`.
- `native/shell.c`: the `output_power` control op calls `weston_output_power_off` / `weston_output_power_on` on the named output (DPMS, never disable) and answers the event; never a request in `photo-wall-frame-v1.xml` (E-1B-2).
- `runner.main()`: builds the three adapters (production `Runner` = `subprocess.run(argv, capture_output=True, text=True, timeout=t, env={"XDG_CACHE_HOME": "/tmp/ddcutil", "PATH": "/usr/bin"})`), an `OutputPowerController` with `BusReportSink`, feeds it `session.desired.on_change` (decoded documents; an undecodable one is logged and ignored) and every Output observation.
- Unit `photo-wall-display-controller.service`: `PrivateDevices=yes` removed; `DevicePolicy=closed`, `DeviceAllow=char-cec rw`, `DeviceAllow=char-i2c rw`; `SupplementaryGroups=pw-node-feeds video i2c`; `MemoryMax` and `TasksMax` checked against the session thread, the worker thread and one tool child (measure; change `appliance/kernel/capacity.py`'s line only if the cap must move).
**Acceptance:** `tests/node/display/test_display_power_methods.py` (unit; fake `ddcutil` and `cec-ctl` executables on a temp PATH that record argv and answer from a scripted display; a fake clock): DDC/CI off sends `setvcp D6 4` then reads back and reports `confirmed`, on sends `setvcp D6 1`; a DDC display that does not answer → `did-not-answer`; CEC off sends `--standby` and reads `--give-device-power-status`, "Not Acknowledged" → `did-not-answer`; signal off → `signal-stopped` through a fake compositor; best detected picks CEC over DDC/CI over signal off; none answers → `not-supported`; #118 with another input → `another-input`, cannot tell → the off goes ahead; read-before-write: a display already off gets no write and `confirmed`; a blip (disconnect then reconnect, same identity, display reads standby) writes nothing; a `console-test` off of 300 s ends on the fake clock with no new document (Central away) and the standing on is applied; a restart with the document present acts once; a hanging tool returns `did-not-answer` within its timeout; no argv ever contains `D6 5`. Unit test in `test_node_display_runner.py`: the controller unit has no `PrivateDevices=yes` and has the two `DeviceAllow` lines and the groups. `tests/node/health/test_judge_output_power.py` (guard, green before and after, E-1B-5): an Output disconnected for a blip, or for the length of a requested off, raises no display-affecting code. `tests/test_node_display_native.py` (CI linux-media, native image): the `output_power` op turns the output's power off and on.
**Mutation probes:** write without reading first → the read-before-write case fails; restart a running count when the same request is re-put in a new bus epoch → add and use the case "a re-put of the same request keeps its count"; map `best-detected` DDC/CI before CEC → the precedence case fails; send D6 5 for off → the never-5 assertion fails.
**Land:** `feat(display): CEC, DDC/CI and signal off carry out the Output document (1b D2)`.

## C1: Displays and identity (lane central)

**Owned:** see the lane table.
**Design:** revised.md rule 1, "Contracts" row "Central: Displays", "Data flow"; owner q2; PARKED "Central side", "Reconcile with existing code".
**Frozen page:**
- Migration `072_displays.sql` (forward-only):
  - `displays(id UUID PRIMARY KEY, maker TEXT NOT NULL CHECK (maker ~ '^[A-Z]{3}$'), product INTEGER NOT NULL CHECK (product BETWEEN 0 AND 65535), name TEXT NOT NULL, serial TEXT NULL, frame_id TEXT NULL REFERENCES frames(id) ON DELETE CASCADE, modes JSONB NOT NULL DEFAULT '[]', power_method TEXT NULL CHECK (power_method IN ('hdmi-cec','ddc-ci','signal-off')), switch_input_on_power_on BOOLEAN NOT NULL DEFAULT TRUE, never_off_on_other_input BOOLEAN NOT NULL DEFAULT TRUE, first_seen_at DOUBLE PRECISION NOT NULL, CHECK ((serial IS NULL) <> (frame_id IS NULL)))`; unique `(maker, product, serial) WHERE serial IS NOT NULL`; unique `(maker, product, frame_id) WHERE frame_id IS NOT NULL`.
  - `shared_serials(maker TEXT, product INTEGER, serial TEXT, found_at DOUBLE PRECISION NOT NULL, PRIMARY KEY (maker, product, serial))`.
  - `output_displays(player_id TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE, output_id TEXT NOT NULL, connected BOOLEAN NOT NULL, identity JSONB NULL, display_id UUID NULL REFERENCES displays(id) ON DELETE SET NULL, report JSONB NOT NULL, reported_at DOUBLE PRECISION NOT NULL, PRIMARY KEY (player_id, output_id))`: `identity` = the last non-null identity reported (kept through an unplug); `display_id` NULL = pending or never seen; `report` = the latest report as received.
  - `output_documents(player_id TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE, output_id TEXT NOT NULL, change BIGINT NOT NULL CHECK (change >= 1), digest TEXT NOT NULL, document JSONB NOT NULL, PRIMARY KEY (player_id, output_id))`: written only by the document source.
  - No trigger and no stored procedure (owner: no DB logic).
- `central/displays/model.py`: C1 implements `DisplayKey`, `Sighting`, `serial_usable`, `display_key`, `DisplaySettings`, `ProjectedOutput`, `project_output` (stack: standing on only until C3), `standing_on`.
- `central/infra/display_store.py`: `class OutputReportJudge` implementing `RecordJudge` for stream `KV_state_display` (decode each Read whose key is an `output-…` report; a refusal is logged with the record's coordinates and skipped; resolves the device to its non-retired player; applies the identity rules in the same transaction; on a bind of an Output holding a pending identity, resolves it); `class DisplayDocuments` implementing `DocumentSources` (its per-link `DocumentSource.documents("KV_desired_display")` returns `{output_document_key(o): encode_output_document(...)}` for each Output of the Node that has an `outputs` or `output_displays` row, raising `output_documents.change` only when the digest differs; every other stream `{}`; `changed()` returns when this Node's facts changed in this process); `class PgDisplayQueries` implementing `DisplayQueries.frame_display`.
- `central/infra/node_links.py`: `NodeLinks(pipe, hub_url, stores, documents: DocumentSources)`, `run_link(..., documents.source(serial, pipe), stop)`.
- `central/infra/node_link_store.py`: `PgLinkStores(db, judges: Mapping[str, RecordJudge] = {})`; `commit` calls the stream's judge with the Reads it inserted (`RETURNING` the inserted keys), after the inserts and before the cursor, in the same transaction.
- `central/node_bus_wiring.py`: wires `DisplayDocuments` for both pipes (the show pipe gets `{}` everywhere) and the judge; `_NoDocuments` stays only as the wall-documents source.
- `central/registry.py`: `bind` resolves a pending identity on the bound Output (one call into the store, same transaction).
- `central/app.py` mounts `mount_display_routes`. In C1 it declares only GET display (the frozen declarations of the other three stay in the file but out of the mount until C3, which mounts them; no flag).
**Acceptance:** `tests/test_display_identity.py` (DB): (1) a display with a usable serial seen on another Pi's Output is the same Display and the `power_method` set before the move is its setting after; (2) a no-serial display on a bound Output is keyed to that Frame; the Frame rebound to a new Pi's port with the same display → the same Display; the display moved to another Frame → a new Display with default settings; (3) **the owner's case:** two identical no-serial monitors (same maker, product, name) on one Pi's HDMI-A-1 and HDMI-A-2, bound to two Frames → two Displays, each tied to its Frame; (4) a no-serial display on an unbound Output is pending (`display_id` NULL) until the Output is bound, then resolves; (5) a report with `identity: null` (blip, unplug) keeps the recorded Display and identity; (6) two Outputs reporting the same maker, product and serial at once → the serial is recorded shared and the second is keyed to its Frame; (7) the source raises `change` only when the body changes. `tests/integration/test_central_display_documents.py` (DB + nats): a Node's report recorded by the link store is judged into `output_displays` in the same transaction, and the Node's desired bucket receives `output-HDMI-A-1` with standing on.
**Mutation probes:** key a no-serial display by (player, output) → case 2 fails; let a null identity clear `display_id` → case 5 fails; drop the shared-serial check → case 6 fails; raise `change` on every call → case 7 fails; key case 3 by (maker, product) only → case 3 fails.
**Land:** `feat(central): Displays recognised from each Output's EDID; the Output document source (1b C1)`.

## C2: readiness from Position commits (lane central)

**Owned:** see the lane table.
**Design:** revised.md rule 3, "Contracts" row "Central: Readiness", Costs ("At deploy…"); PARKED "Readiness", "Profile on a bound Frame"; requirements › Installation model :94-96.
**Frozen page:**
- Migration `073_position_display.sql`: `ALTER TABLE frames ADD COLUMN position_generation BIGINT NULL, ADD COLUMN position_display_id UUID NULL REFERENCES displays(id) ON DELETE SET NULL; UPDATE frames SET position_generation = generation WHERE calibration_valid; ALTER TABLE frames DROP COLUMN calibration_valid;` (every Frame ready before stays ready; no dual path).
- `model.readiness`, `model.adopts`, `FramePosition` as frozen. Every reader of `calibration_valid` reads `readiness(...) == Readiness.READY` (`configuration_in`'s `execution_bindings`, `installation_repository`, `readiness_diagnostics`); `FrameInventory.calibration_valid: bool` becomes `readiness: Readiness`.
- Both Position commits (`calibrate` commit and `commit_calibration_trial_in`) set `position_generation = generation` and `position_display_id` = the Display last seen on the bound Output (NULL when none); `bind`, `unbind` and a profile change raise the generation as today and no longer write a readiness flag.
- The report judge, when it records a Display on an Output for the first time (none before), sets `position_display_id` on the Frame bound there when `adopts(...)`.
- `replace_frame_profile` refuses `frame_bound` only when the Frame is bound and not display-changed.
- Console: `unfinished.js`, `health.js`, `frame-page.tsx`, `frame-profile.tsx` read `frame.readiness` (`"ready"` where they read `calibration_valid === true`); wording unchanged except the display-changed case, which K1 words.
**Acceptance:** `tests/test_display_changed_readiness.py` (DB): a ready Frame whose Output reports another Display → `display-changed` and absent from `configuration_in(...)["execution_bindings"]`; a profile replacement on it is accepted; a Position commit (each of the two paths) → `ready` and back in `execution_bindings`; a blip (null identity) keeps it ready; a first sighting on a Frame committed with no Display → adopted, ready; a second, different Display afterwards → display-changed; migration: a Frame with `calibration_valid = true` before 073 is `ready` after with no report. Existing tests naming `calibration_valid` are moved to `readiness` and stay green.
**Mutation probes:** ignore the Display in `readiness` → the display-changed case fails; adopt on every sighting → the "second Display" case fails; keep the profile refusal → the profile case fails; drop the migration's backfill → the migration case fails.
**Land:** `feat(central): a changed display keeps its Frame not ready until Position is re-checked (1b C2)`.

## C3: power requests and Test (lane central)

**Owned:** see the lane table.
**Design:** revised.md option A, rule 2, "Contracts" row "Central: Power requests", "Data flow"; owner q1; PARKED "Request end".
**Frozen page:**
- Migration `074_power_requests.sql`: `power_requests(id UUID PRIMARY KEY, frame_id TEXT NOT NULL REFERENCES frames(id) ON DELETE CASCADE, power TEXT NOT NULL CHECK (power IN ('on','off')), reason TEXT NOT NULL CHECK (reason IN ('console-test')), for_seconds INTEGER NOT NULL CHECK (for_seconds > 0), created_at DOUBLE PRECISION NOT NULL, ends_at DOUBLE PRECISION NOT NULL)`; unique `(frame_id, reason)` (a new test replaces the last).
- `model.project_output` with the live test on top; `model.power_status` as frozen; `TEST_SECONDS = 300`.
- `display_store`: `PgDisplayCommands` implementing `DisplayCommands` (`request_power_test` upserts the row with `ends_at = now + TEST_SECONDS` on Central's transaction clock; both commands `NOTIFY photo_wall_output_documents, '<device_id>'` from app code in the same transaction); `PgDisplayQueries.frame_power`; the source's `changed()` also returns on that notification (the worker `LISTEN`s on one connection) and at the earliest live `ends_at` of its Node.
- `display_routes`: the three remaining routes mounted.
**Acceptance:** `tests/test_display_power_requests.py` (DB): a test off on a bound Frame → 202 and the projection has the test on top of standing on; Turn on replaces it (one row); an unbound Frame → 409 `frame_unbound`; past `ends_at` the projection drops it and `change` rises; `power_status`: waiting until a report names the change carrying the test, then answered with its result; a report naming an older change keeps waiting. `tests/integration/test_central_display_power.py` (DB + nats): POST a test → the Node's desired bucket holds the document with the test within 3 s (the notification path, no reconcile).
**Mutation probes:** drop the NOTIFY → the 3 s case fails; compute `answered` from `result` alone → the older-change case fails; keep expired tests in the projection → the `ends_at` case fails.
**Land:** `feat(central): Test off and on as a timed power request in the Output document (1b C3)`.

## C4: Central notices a silent Player, test-first (lane central)

**Owned:** `tests/test_central_silent_player.py` (DB tier), `tests/browser/test_silent_player_browser.py`. No production file unless a re-cut adds it (below).
**Design:** owner q3 ("Central notices a silent Player"); requirements U4 ("Central distinguishes a silent Player application from an unobserved Host Management agent, whether or not the Player is bound to a Frame"); `contracts/liveness.py`; the root-cause report, `/Volumes/Dock/tmp/display-1b/player-root-cause/README.md` (c).
**Root cause: UNPROVEN** (E-1B-15). Central already works out silence when it reads: the snapshot serves `last_report_at` and `silent_after_seconds`, `health.js` classifies a Frame's Player as `silent`, and the Player page shows its layers. Whether that reached the owner during the incident was not checked (the database read was denied). So this slice's first and possibly only deliverable is the replay.
**Frozen page:** no new signature. The replay, from the incident (bound Frame on HDMI-A-1; the Player's last accepted report at T; the host's observations keep arriving; the broker's `AppProcessFact(exited)` ingested with no app link, so its work stays `awaiting_output_link`), read at T + `SILENT_AFTER_SECONDS` + 1 on Central's test clock:
- `tests/test_central_silent_player.py`: (1) `test_a_bound_player_that_stops_reporting_reads_silent_while_its_host_reports`: the operator snapshot's Player has `last_report_at == T`, `read_at - T > silent_after_seconds`, and its host observation is current (`central/fleet/host_thresholds.py`); (2) the same for an unbound Player (U4: "whether or not … bound"); (3) `test_an_app_exit_with_no_app_link_is_readable_on_the_player`: the exit's evidence, with Central's `received_at`, is in the Player page's node read (whatever `central/console/src/nodeRead.js` `layerEvidence` reads), not only in a pending work row.
- `tests/browser/test_silent_player_browser.py`: on the same state, the Wall marks that Frame with the liveness cause and links to the Player page; the Player page shows the Player app as not heard from lately while Host Management reads current.
**Outcome rule.** Every case green on the branch: land them as CI guards, and say in the commit body that Central already notices; the dark wall was the Pi's (P1a, P1b). X1 records it. Any case red: append `E-1B-C4-1` with the failing assertion and its output, push the tests on `wip/1b-C4`, and STOP. The code architect then freezes the fix's signatures and re-cuts. Candidate files: `central/installation_models.py`, `central/operator_snapshot.py`, `central/node_runtime_reconciliation.py`, `central/fleet/node_observations.py`, `central/console/src/health.js`, `central/console/src/nodeRead.js`, `central/console/src/PlayerPage.jsx`.
**Not in 1b (stated cost).** The Node's own verdict (`app_absent`, P1b) does not reach Central. That is E7's health line (`STORE_LINES["health"]`, declared, unused). Until then Central reads silence from the Player's reports and exits from the broker's evidence.
**Mutation probes (green case):** serve no `silent_after_seconds` → (1) fails; have `health.js` read a silent Player as heard → the browser case fails.
**Land:** `test(central): Central's reading of a silent Player, replayed from the 2026-10-09 incident (1b C4)`.

## P1a: the Player releases what it opens (lane player-reliability)

**Owned:** see the lane table. Siblings share the checkout: P1b runs at the same time in `appliance/`; keep to these paths.
**Design:** owner q3 (2026-10-10: "the Player survives its HDMI output going away and coming back (signal blip), … the 'too many open files' (EMFILE) fault is root-caused and fixed"); the root-cause report `/Volumes/Dock/tmp/display-1b/player-root-cause/README.md` (a), (b) and its repro scripts.
**Root cause.**
- (b) is proven: every decoder leaks its pipeline through closures that hold the decoder.
- (a) is proven only in a container: a remap under fd exhaustion makes Mesa's software EGL SEGV. The Pi's own HPD bounce was not reproduced (headless Weston cannot unplug, and there was no core dump).
- **First deliverable:** the harness below with its two red tests. Its red output on the base goes in the commit body.

**Frozen page (exact; `player/native.py` internals named because they are the fix):**
- `_Decoder` gains `handlers: list[tuple[Any, int]]` (every `(GObject, handler id)` this decoder connected: the appsink's `new-preroll` and `new-sample`, demux `pad-added`, the bus `message`) and `probes: list[tuple[Any, int]]` (every `(Gst.Pad, probe id)`). `bus_handler` is folded into `handlers`.
- Every closure `_new_decoder` connects captures `weakref.ref(decoder)` and module objects only; never `decoder`, `pipeline`, `sink` or `self` strongly. A dead reference returns `Gst.FlowReturn.OK` / `Gst.PadProbeReturn.OK` and does nothing. Every connect and probe goes onto the decoder's lists in the same statement that makes it.
- `_destroy_decoder(self, decoder: _Decoder) -> None` stays the one teardown. Callers: `_new_decoder`'s PAUSED refusal, `prepare`'s retry, `release`, `close`. It closes the mailbox, disconnects every handler, removes every probe, removes the bus signal watch, sets the pipeline to NULL, and drops `sample`, `bus` and the lists. A second call is a no-op.
- `_Surface` failure is scoped to an **episode**:
  - New fields: `episode: int = 0`, `failed_episode: int | None = None`, `grant_key: tuple[UUID, int, int] | None = None`.
  - New methods: `fail(self, reason: str) -> None` (sets `failure` and `failed_episode = episode`), `failed(self) -> str | None` (returns `failure` while `failed_episode == episode`; otherwise clears both and returns None), and `begin_episode(self) -> None` (`episode += 1`).
  - A new episode begins on every `map` of the window (`_window_realized` on map) and whenever `_render` or `present` sees a grant whose `(grant_id, binding_generation, config_revision)` differs from `grant_key`.
  - Every read of `surface.failure` in `native.py` goes through `failed()`, and every write through `fail()`. The `wayland_app_id` refusal at construction still raises (it fails the first episode).
- `player/output_discovery.py`: `CONFIGURED_OUTPUT_IDS` is deleted; `weston_ini` and `discover_outputs` iterate `contracts.node_output.OUTPUT_IDS` (no alias).
- `uplink/causes.py`:
  - `Cause.LOCAL = "local"` (comment: this machine ran out of a resource; nothing about the network was learned).
  - `REASONS[Cause.LOCAL] = frozenset({"descriptors", "memory"})`.
  - New rules, first in both phases: `errno in (EMFILE, ENFILE)` → `(LOCAL, "descriptors")`; `errno == ENOMEM` → `(LOCAL, "memory")`.
  - `classify` stays total over OSError. The Player's fault then reads `local_descriptors`, matching `player/service.py`'s own rule that "a local failure … is never reported as a network cause". Cost: E-1B-19.
- `scripts/run_display_harness.py`: `run_argv` runs `… && /usr/bin/python3 /smoke/native_player_resource_harness.py` after the main-loop harness, and its docstring says so. `scripts/release_plan.py`: the node-pid1 suite's `paths` gain `tests/native_player_resource_harness.py`.

**Acceptance (each red on the base, green after; the harness runs in CI's node-pid1 `display-harness` job, about 10 s):**
`tests/native_player_resource_harness.py` follows `native_player_mainloop_harness.py`'s shape: its own headless Weston (kiosk, pixman), `LIBGL_ALWAYS_SOFTWARE=1`, the real `NativeRenderer`, `PASS <name>` per case, and JPEG fixtures made in the harness with GStreamer (`videotestsrc num-buffers=1 ! jpegenc`). Its cases:
1. `decoder_churn_releases_every_descriptor`: prepare and release 50 JPEG decoders through `NativeRenderer.prepare`/`release`. After `gc.collect()`, `/proc/self/fd` is back at its baseline count and no `Gst.Pipeline` made by the churn is in `gc.get_objects()`. Today: +100 fds, 50 pipelines.
2. `slideshow_then_output_remap_survives_fd_limit`: in a child process (`--remap-child`), set the soft `RLIMIT_NOFILE` to 256 and run 150 slide changes on one Output (prepare, present, release). Then `hide()` and `show_all()` the window, and the next `present()` returns `"presented"`. The parent asserts rc 0 and the child's last line. Today the child dies with −11.
3. `a_failed_surface_presents_again_after_a_remap`: one `_render` is made to raise once (wrap `_compose` for one call), so `present()` fails. After hide and show, `present()` returns `"presented"`. Today it stays failed.
4. `a_failed_surface_presents_again_under_a_new_grant`: `renderer._frames` is a fake whose `grant(...)` answers an admitted `FrameGrant` and whose `tag_rendered_buffer` and `close` do nothing. A failure under grant G1, then the fake switches to G2 (a new `grant_id`), and the next `present()` returns `"presented"`. Today it stays failed.
- `tests/test_uplink_causes.py::test_descriptor_and_memory_exhaustion_are_local_in_both_phases`: EMFILE and ENFILE give `local`/`descriptors`, and ENOMEM gives `local`/`memory`, in both phases and when wrapped as a `__cause__`; ECONNREFUSED still gives `connect`/`refused`.
- `tests/test_player_service.py`: the Output-id assertion reads `OUTPUT_IDS`.

**Mutation probes:**
- `sample_ready` captures `decoder` strongly again → case 1 fails.
- `_destroy_decoder` skips the handler disconnects → case 1 fails.
- `failed()` ignores the episode → cases 3 and 4 fail.
- No new episode on a new grant → case 4 fails.
- The LOCAL rules move after `_of(OSError)` → the uplink case fails.

**Bench (NOT RUN in the PR; after deploy, with D2's bench step):**
- The Player's `/proc/<pid>/fd` count stays flat over 30 minutes of slideshow.
- A DDC/CI standby HPD bounce leaves the same Player run presenting.

**Land:** `fix(player): decoders release their pipelines, a surface failure ends with its episode, EMFILE is a local cause (1b P1a)`.

## P1b: the Pi restarts a dead Player, and the judge sees it (lane player-reliability)

**Owned:** see the lane table. Siblings share the checkout: P1a runs at the same time in `player/` and `uplink/`; keep to these paths.
**Design:** owner q3 ("the Pi restarts a Player that dies"); the root-cause report (c) and (b) "Detection". Errata E-1B-12, -13, -16, -17, -18. Prior art: Kubernetes CrashLoopBackOff, systemd `RestartSteps=`. The broker stays the one supervisor; why not `Restart=` is in `broker.py`'s F0b comment.
**Root cause:** (c) is proven by the code (`relaunch_for_display` fires once per Weston incarnation) and by the unit's properties (`Restart=no`). **First deliverable:** the red tests replacing the "no loop" assertion.

**Frozen page:** F0b's signatures in `broker.py`, `descriptors.py`, `lifecycle_storage.py`, `judge.py` and `app_facts.py`, their docstrings being the contract. In addition:
- `broker.py`:
  - `relaunch_for_display` is deleted (no alias).
  - `AppProcessDriver.launched_display()` becomes `launched(self) -> Launch | None`.
  - `AppEffectBroker.__init__(…, journal, driver, pacing: RelaunchPacingStore, now_ms: Callable[[], int])`, all keyword-only and required.
  - `reconcile`: when the app is absent and the record is `running`, `exited`, or `effect_unknown` with fault `relaunch_outcome_unknown` (E-1B-18), it calls `relaunch_absent` with the intent journaled as today. None → `exited`/`process_exited`; a run → `running`; a raise → `effect_unknown`/`relaunch_outcome_unknown`.
- `online_broker.py`:
  - `OnlineEffectBroker(store, driver, session, recovery, *, pacing: RelaunchPacingStore, now_ms: Callable[[], int])`.
  - In `reconcile` for `running`/`fallback_running`: when the app is absent, `not self._controlled(command) and not display_took_app(driver.launched(), driver.display_incarnation())` goes to `_settle` unchanged (fallback, then `effect_unknown`; E-1B-17). Otherwise `relaunch_absent(driver, pacing, environment, command.operation_id, now_ms=…)`, with `environment` the target for `running` and the fallback for `fallback_running` (as today), and a raise is `effect_unknown`/`relaunch_outcome_unknown` as today. When the app is present, `_settle` runs as today.
  - `OnlineRunner(store, driver, session, recovery, *, worker, pacing, now_ms, prepared=PREPARED)` passes both through.
- `process_linux.py`:
  - `start` writes `"started_ms": boottime_ms()` into the `launch` record, and `launched()` reads `Launch(epoch, display, started_ms)` from it (records are per boot, so an old record without `started_ms` cannot exist after the reboot that installs this).
  - The app unit's `Environment=` gains `PYTHONFAULTHANDLER=1`.
  - The `app_unit_properties` and `kill` docstrings say the broker relaunches on its pacing; `Restart=no` stays.
- `broker_runner.py`:
  - `BrokerLoop.__init__(self, *, broker, online, store, session, driver, links, probes: ProbeThread, feeds: FeedListener, clock: Callable[[], int] = boottime_ms, proc: Path = Path("/proc"))`, with new state `last_run: AppRunKey | None = None` and `sampled_at: int | None = None`.
  - On every known turn, after `_observe`, Central session or not:
    - When `run != last_run`, append `APP_EXITED {"run": last_run.document()}` if `last_run` is set, then `APP_STARTED {"run": run.document()}` if `run` is set, then set `last_run = run`.
    - While `run` is set and `clock() - sampled_at >= DESCRIPTOR_SAMPLE_MS` (or never sampled), `descriptor_use(proc, pid, start_ticks)`. A use appends `APP_DESCRIPTORS {"run", "open", "soft_limit"}`; None or OSError appends nothing. `sampled_at = clock()` either way.
  - `main()` builds one `FileRelaunchPacing(store)` and passes it, with `boottime_ms`, to both owners.
- `judge.py`:
  - `descriptor_pressure` gets its body.
  - `observe` handles the three kinds (malformed values ignored, as today):
    - `app_exited`: opens `app_absent` pending for that run (`run_changed` if already open for another run), and closes that run's `app_resource_exhausted` as `cleared`/`run_changed`.
    - `app_started` for a run other than the condition's: withdraws a pending `app_absent` (`started`), or clears a raised one (`started`, hold 0).
    - `app_descriptors` for the run that holds the condition, or any run when none is open: under pressure, `app_resource_exhausted` is raised at once (`descriptor_pressure`), or its clear hold restarts; without pressure, the clear hold starts (`descriptor_relief`).
  - `forget` keeps its generic rule.
  - The module docstring drops "M1 has no restart" and states both codes. The `Transition.reason` comment lists `app_exited`, `started`, `descriptor_pressure` and `descriptor_relief`.

**Acceptance (each red on the base, green after; unit tier unless marked):**
- `tests/node/test_node_control_m1.py`:
  - `test_an_app_that_exits_under_the_same_display_starts_again_with_backoff` (replaces the "no loop" lines): on a fake clock, with each run exiting after 2 s, the relaunches start at +1 s, +5 s, +30 s, +300 s, +300 s. A run that lasted 600 s waits 1 s again, and each start has a new epoch.
  - `test_an_exit_is_counted_once_however_many_turns_see_it`.
  - `test_a_new_display_incarnation_starts_the_app_at_once`.
  - `test_a_failed_relaunch_is_paced_and_retried_once_the_unit_is_collected` (replaces `…_never_retried`).
- `tests/node/apps/test_node_online_broker.py`: `test_a_controlled_switch_app_that_exits_starts_again_with_backoff`, and the guard `test_an_uncontrolled_target_that_exits_still_falls_back` (green before and after).
- `tests/node/apps/test_broker_app_facts.py`: one `app_exited` and one `app_started` per run change, also with no Central grant. One `app_descriptors` per `DESCRIPTOR_SAMPLE_MS` on a fake clock from a fake /proc tree; a pid whose start ticks changed appends none.
- `tests/node/apps/test_app_descriptors.py`:
  - `parse_soft_limit` on a verbatim Linux limits text gives 1024. A missing row, a doubled row and `unlimited` are each `ValueError`.
  - `descriptor_use` on a fake proc tree counts the entries; changed start ticks give None.
  - A Linux-only real read of the test process.
- `tests/node/health/test_app_absent.py`:
  - `test_app_exit_raises_app_absent_until_a_new_run_starts`: pending at the exit, raised at 5,000 ms, with the tint and "Photos paused — the player stopped" on a connected Output. `app_started` for another run clears it; within the window it withdraws it, never shown. `app_started` naming the exited run changes nothing.
  - `test_descriptor_pressure_raises_and_clears_resource_exhausted`: 820 of 1,024 raises at once and 819 does not. 60,000 ms below clears it, and so does the run's exit. It is never display-affecting.
  - `test_a_feed_gap_keeps_a_raised_app_absent`.
- `tests/node/test_node_linux_adapters.py`: the app unit carries `PYTHONFAULTHANDLER=1`, and `launched()` returns the `started_ms` that `start` wrote.
- **Qualification, node-pid1 tier** (CI `scenario: success` leg; under 20 s added): after the success scenario's last assertion, `systemctl kill --signal=SIGSEGV photo-wall-node-player.service` inside the container. Within 15 s the unit is active again with a new `InvocationID`, the broker's `launch` epoch is one higher, and the kernel boot id is unchanged.

**Mutation probes:**
- `relaunch_absent` returns None when the display is unchanged (the old rule) → the backoff test fails.
- `STABLE_RUN_MS` is ignored → its reset case fails.
- `pace_relaunch` counts the same epoch again → counted-once fails.
- The online broker relaunches before checking control → the fallback guard fails, and so does the PID1 `failure` leg.
- `app_started` of the exited run clears → its case fails.
- `>` instead of `>=` in `descriptor_pressure` → the 820 case fails.
- `PYTHONFAULTHANDLER` is dropped → the adapter case fails.
- `app_exited` is emitted only inside the Central-session branch → the no-grant case fails.

**Not in P1b (stated):**
- systemd-coredump becomes a residual after I1 (E-1B-14).
- A switch still `preparing` keeps the previous launch, and a death in that window is not relaunched until the switch settles. That is a residual risk; it was not observed.
- The existing probe feed kinds stay bare strings: a residual.

**Bench (NOT RUN in the PR; after deploy):**
- `systemctl kill -s SEGV` on the test Pi's Player brings a new run within about 2 s, and the wall shows photos again.
- Check whether the render-node grant (PR 63) is live: `software_renderer` raised on the Pi means it is not, and the Player still draws with llvmpipe.

**Land:** `fix(apps): the broker restarts a dead Player on a backoff; the judge raises app_absent and descriptor pressure (1b P1b)`.

## K1: Hardware tab Display and the display-changed card (lane console)

**Owned:** see the lane table.
**Design:** `.claude/skills/console-ux/SKILL.md` (procedure; the rules are design-language.md); DL §8 row G5 (ProblemCard `todo`; Position re-check Live; Done clears the block); operator-console-design §5 Hardware, §6; revised.md rule 3.
**Frozen page:** `displayApi.js`: `export async function readFrameDisplay(frameId: string): Promise<{ok: true, view: FrameDisplayView} | {ok: false, error: string}>` over GET display. `domain/frame-display.tsx`: `export function FrameDisplay(props: {frameId: string}): JSX.Element` (Display: make, model, serial or "Recognised by make and model on this Frame", detected modes read-only), and `export function DisplayChangedCard(props: {view: FrameDisplayView, onConfirmProfile: () => void, onRecheckPosition: () => void}): JSX.Element` shown when `readiness === "display-changed"`: step 1 confirm or correct the profile (pre-filled from the new Display's preferred mode), step 2 re-check Position. Copy never says Panel, Actuator, Display Host or calibration (DL :492).
**Acceptance:** `tests/browser/test_frame_display_changed_browser.py`: a Frame whose Output reports another Display shows the card with both steps; confirming the profile then committing Position clears the card and the Frame reads ready; an unchanged Frame shows no card; a no-serial Display says it is recognised by make and model on this Frame. Typecheck, lint, the catalog leg for new stories.
**Mutation probes:** show the card on `position-needed` too → the unchanged-Frame case fails; skip pre-filling the profile → its assertion fails.
**Land:** `feat(console): the Hardware tab shows the Display and the display-changed card (1b K1)`.

## K2: the Power tab (lane console)

**Owned:** see the lane table.
**Design:** DL §8 row B6 (SettingRow, Button Test, AckBadge, LinkToOwner; Autosave options + Action Test; "Display confirmed off" / "Display didn't answer"; EmptyState when no method); operator-console-design §5 Power tab, #116-#118; revised.md R7.
**Frozen page:** `powerApi.js`: `readFramePower(frameId)`, `requestPowerTest(frameId, power: "on" | "off")`, `savePowerSettings(displayId, settings: DisplayPowerSettings)`, each returning `{ok: true, …} | {ok: false, error}` through `apiWrite`. `domain/frame-power.tsx`: `export function FramePower(props: {frameId: string}): JSX.Element`. `TABS` (frame-page.tsx and routes.js) become overview, position, picture, power, hardware (design §5: Power between Picture and Photo fit). Result wording: `confirmed` + off → "Display confirmed off", + on → "Display confirmed on"; `signal-stopped` → "Picture stopped; the display may sleep"; `did-not-answer` → "Display didn't answer"; `not-supported` → EmptyState naming the smart plug (later); `another-input` → "Left on: the display is showing another input"; `waiting` → "Waiting for the Pi"; not linked → "Last heard from the Pi at …" (`last_heard_at`). Unbound → "Choose which Pi and HDMI port feeds this Frame".
**Acceptance:** `tests/browser/test_frame_power_browser.py`: the tab is reachable at `#/wall/<frame>/power`; Test: turn off posts once and shows "Waiting for the Pi" until a recorded report answers the change, then "Display confirmed off"; Turn on replaces it; an unanswered test shows "Display didn't answer" from a `did-not-answer` report; a method change autosaves; an unbound Frame shows the binding prompt. `tests/test_console_routes_r4.py` pins the new `TABS`.
**Mutation probes:** show the result before the report names the change → the waiting case fails; drop the power tab from routes.js `TABS` → the routes pin fails.
**Land:** `feat(console): the Frame's Power tab with Test off and on (1b K2)`.

## X1: docs (lane docs)

**Owned:** see the lane table. **Design homes to change** (revised.md "Findings to report"; owner q2): console design §3 and §6 (no-serial rule: make and model tied to the Frame; the owner's two-identical-monitors case), §5 Power and Hardware, catalogue #116-#118 and #120 wording; DL §8 rows B6 and G5 (:540, :578-579, "no identity: treated as new" replaced); requirements.md only where an owner answer changed a written rule (none expected: 0019 is proposed, so these are design-home edits; say so); execution-contract.md: power is operational state, outside a Frame's content after-states; roadmap 1b status cells, the acceptance line for `test_display_identity.py`, the stale 1a test names; runbook: the display line, the controller's device access; AGENTS.md code map rows (`central/displays/`, `appliance/display_host` power); `.claude/skills/display-control/SKILL.md` from the spike's runbook (access, port to bus mapping, EDID, CEC, DDC/CI, traps, the spike-tools recipe). Every rule in one home; others link. `python3 scripts/check_docs.py` green.
**Land:** `docs: display identity and power (1b X1)`.

## Outcome (filled by the run's closing step)

| Slice | Outcome | sha |
|---|---|---|
| F0 | landed | (this commit) |
| F0b | landed | (the player-reliability freeze commit) |
