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
| central | **C4 (placeholder)** | Central notices a silent Player (owner q3) | page written from P1's root-cause report; likely `central/fleet/` or `central/health/`, a migration `075_*` if it stores anything, its tests | P1's report, C3 | K2 |
| player-reliability | **P1 (placeholder)** | Player survives an HDMI blip; the Pi restarts a dead Player; the EMFILE leak root-caused and fixed (owner q3) | page written later from a separate root-cause report; likely `player/`, `appliance/apps/`, `appliance/health/`, `tests/node/apps/`, `tests/test_player_*.py`, `tests/node/test_health_judge.py` | F0 | every slice outside its seams (below) |
| console | **K1** | Hardware tab: Display and the display-changed card | `central/console/src/domain/frame-display.tsx`, `central/console/src/domain/frame-display.stories.tsx`, `central/console/src/displayApi.js`, `central/console/src/pages/frame-page.tsx`, `tests/browser/test_frame_display_changed_browser.py`, `central/console/dist/` (its build) | C2 | D2, C3, P1 |
| console | **K2** | Power tab: method, switches, Test off/on | `central/console/src/domain/frame-power.tsx`, `central/console/src/domain/frame-power.stories.tsx`, `central/console/src/powerApi.js`, `central/console/src/pages/frame-page.tsx`, `central/console/src/routes.js`, `tests/test_console_routes_r4.py`, `tests/browser/test_frame_power_browser.py`, `tests/browser/catalog-baselines/` (new stories' baselines), `central/console/dist/` | C3, K1 | C4, P1 |
| docs | **X1** | Design homes updated to what landed; the display-control skill | `docs/operator-console-design.md`, `docs/design-language.md`, `docs/execution-contract.md`, `docs/requirements.md` (only where an owner answer changed a written rule), `docs/roadmap.md`, `docs/runbook.md`, `AGENTS.md`, `.claude/skills/display-control/SKILL.md` | drafts from the pages at once; final pass after K2, D2, C4, P1 | everything |

**Fan-out.** T1 and I1 start together. After T1 lands: D1, C1 and P1 fan out (disjoint packages: `appliance/display_host`, `central` + `nodeapi`-free, `player` + `appliance/apps`), I1 continues. Inside a lane, slices run in order (D1 then D2; C1 then C2 then C3). K1 waits for C2 (it reads `readiness`, and C2 edits `frame-page.tsx`), K2 for C3 and K1. Only the central lane adds migrations and touches `central/` Python; only T1 touches `contracts/` after F0. `central/console/` belongs to the console lane except the four files C2 migrates off `calibration_valid` in the same compile unit (co-change, skill §3.2): C2 lands before K1, so they never overlap.

**Seams with player-reliability (P1).** (a) The HDMI blip tears down and rebuilds the Weston Output; if P1's root cause sits in `appliance/display_host/weston.py` or `native/shell.c` (D2's files), P1 appends an erratum and the fix lands in D2 or in a P1b sequenced after D2, never in parallel edits. (b) "Central notices a silent Player" is Central code: C4, in the central lane. (c) `appliance/health/` is P1's; D2's judge test only reads it (E-1B-5). (d) P1 adds no contract; it points `player/output_discovery.py` `CONFIGURED_OUTPUT_IDS` at `contracts.node_output.OUTPUT_IDS` (rule of two). (e) "A blip is not a display change" (C1, D2) and "the Player survives a blip" (P1) are separate tests of separate behaviours.

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

**Budget gates.** Per slice: 90 min wall-clock (120 for T1, C1, C2, D2), at most 8 agents. Feature: 12 h wall-clock and 8 M tokens; warning at 80 %, stop after the current slice at 100 %. At most two fix cycles per gate, then a pushed `wip/1b-<slice>` branch and stop. A course-correction architect pass after the fifth implementer (owner memory) and before the end review. Models pinned by id at launch: `claude-opus-5-5` for T1, D1, D2, C1, C2, C3, K1, K2, P1, C4; `claude-sonnet-5-5` allowed for I1 and X1 (owner, 2026-10-09).

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

## C4 (placeholder): Central notices a silent Player (lane central)

Page written by the code architect from P1's root-cause report, before C4 starts. Known so far: owner q3 scope "Central notices a silent Player"; requirements U4 ("Central distinguishes a silent Player application"); the crash evidence (no log from the broker, manager or health service after the SEGV). Owned paths, migration (075 if any) and tests are named then. Depends on P1's report and C3 (migration order).

## P1 (placeholder): Player reliability (lane player-reliability)

Page written later from a separate root-cause report. Scope (owner q3, 2026-10-10): the Player survives its HDMI output going away and coming back (a signal blip); the Pi restarts a Player that dies; the "too many open files" (EMFILE) fault is root-caused and fixed (Central's side is C4). Evidence: `/Volumes/Dock/tmp/display-1b/pi-crash-evidence.log`, `pi-journal-boot.log.gz` (Player SEGV at 2026-10-10T16:06:37Z right after a DDC standby HPD bounce; about 1,000 EMFILE faults since 2026-10-09 23:34). Likely owned: `player/`, `appliance/apps/`, `appliance/health/`, `tests/node/apps/`, `tests/test_player_*.py`, `tests/node/test_health_judge.py`, plus `player/output_discovery.py` pointing at `contracts.node_output.OUTPUT_IDS`. Seams: see "Seams with player-reliability" above. Each test red before, green after; the "survives a blip" scenario is a CI test (owner: qualification scenarios are CI tests), likely a PID1 or linux-media scenario.

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
