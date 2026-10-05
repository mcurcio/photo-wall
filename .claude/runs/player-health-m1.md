# Player health M1 — run brief (G7 seam, tracer part 1)

Working state for an unattended delivery run. Binding process: `~/.claude/skills/implementation-workflow/SKILL.md`. Binding frame: `player-shape-final.md` r8 (owner-accepted 2026-10-04) and the module design r9 (`player-module-design-r8.md`, history line r9). Both live in the orchestrator's scratchpad; **copy them to `docs/design/player-health/` in the first landed commit (B0)** so the plan of record is in the repo. Errata: `.claude/errata.md` (append-only, read with `grep -a`). Branch: `claude/player-health-tracer` (base `origin/main` 860465c). Owner instruction: "Start the build after the planning is complete."

## 1. Goal and scope

Deliver **M1 only**: the G7 seam on today's Central path. Observable at the end: the node-pid1 leg `unresponsive` boots a real Player, Central admits it to a bound Frame, the Player answers progress probes from its GLib control queue, a starved Player (rendering on, control queue starved) is judged `app_unresponsive`, the health overlay client draws a tint and card on the health layer **above the still-live app** and reports that serial presented, the broker kills the Player after K, the shell revokes, and the slate shows under the card.

**Stop at the M1 coherence checkpoint**: after B12, D1, architect pass 3 and a green milestone gate, post the handoff and END. Do not start M2.

Non-goals (M1): Authority / Central link, permits, `node_health` at Central, restarts after kill, `app_failed`, judge state persistence, other fault codes, compositor watchdog, tty1 text, mute, intent, dark periods, console.

## 2. Fences (a bead that needs to cross one STOPS and reports)

1. **Q1 fence.** No change to `appliance/node/recovery.py`, `appliance/node/recovery_linux.py`, or the arming in `appliance/node/online_broker.py` (:98-108 obligation and `_arm_recovery`, :183-184, :211). The broker **never kills while a recovery may be armed** (B8 predicate). Restart-with-backoff is M3. The tracer ends at kill, then slate.
2. **No Authority extraction.** No `appliance/authority` package, no Central link unit, no new uid for Central I/O. `appliance/display_host/service.py` keeps its Central worker (only its import paths change in B2a). The broker keeps its own `app_effect_broker` session and delivers its own reports.
3. **No Central changes.** Nothing under `central/` changes in M1. Bead B1 is the only bead allowed to need Central behaviour, and its fixture binds a Frame from test code through existing Central/Registry APIs. If B1 finds that admission needs a Central code change, that is a STOP with evidence, not an edit.
4. No migrations. No edits to an applied migration.
5. No production Player code path exists only for tests: the starvation seam is a fixture-derived role (B12).
6. `uv.lock` unchanged at every commit (revert if `uv run` touched it).

## 3. Preflight (coordinator, 2026-10-05) — READY-WITH-LIMITS

| Item | Result |
|---|---|
| Host | macOS arm64. Docker daemon up, `linux/arm64`, privileged containers work |
| Native tooling | meson/pkg-config absent; Weston cannot build on Darwin. **Every C, native or harness check runs inside a Linux arm64 Docker container** (Debian trixie, the node build image) via `scripts/run_display_harness.py` (created in B0), or in CI |
| node-pid1 fixture | cold build (`scripts.build_node_components` + `scripts.build_node_pid1_fixture`) estimated 15–30+ min, no BuildKit cache; build once in B1, then warm rebuilds per behaviour-changing bead |
| Node | `source ~/.nvm/nvm.sh && nvm use 20` required (default is v16) |
| Baseline | ruff clean; lint-imports 7 kept; check_docs 121 docs; unit 3649 passed / 24 skipped; DB 937 passed / 16 skipped / 1 xfail (test DB compose already running) |
| Hooks | none |
| gh | authed (repo, workflow) |
| Scratch | `/Volumes/Dock/tmp` (basetemp, TMPDIR, UV cache, fixture builds) |
| Browser tier | not exercised in preflight: the milestone gate checks `playwright install chromium` first; an environment failure there is reported as an explicit skip, not a code stop (M1 touches no console) |
| Models | Pinned by agent type (observed in this session's run metadata, 2026-10-05): system-architect, design-reviewer, implementer = `claude-opus-5-5`; verifier = `claude-sonnet-5`. Orchestrator = `claude-opus-5-5`. |

## 4. Budget gates (hard; the run stops itself)

- **Per bead:** 90 min wall-clock, 8 agents. Exceeding either = stop after the bead, report.
- **M1 tokens:** ceiling **9.5 M**, warning at 7.6 M (80 %). Basis: 16 beads × 0.45 M (implement 0.25 + verify 0.15 + orchestration 0.05) = 7.2 M; 6 review lenses × 0.15 M = 0.9 M; architect passes 1–2 × 0.3 M = 0.6 M; pass 3 + milestone gate 0.6 M; total 9.3 M. Cross-check: the r8 draft budgeted 0.65 M/bead all-in; this is 0.59 M/bead.
- **M1 wall-clock:** ceiling **26 h**, warning at 20.8 h. Basis: 13 leg-bearing beads × 85 min (implement 35, verify 20, warm fixture rebuild 12, one or two legs 15) + B0 80 min + B2c 40 min + D1 40 min ≈ 21 h; architect passes 2 × 45 min; milestone gate 2.5 h (local suites, seven legs incl. the mutation variant, one CI round). Expect about 7–8 beads landed in a 10-hour night; the run stops cleanly wherever a gate falls.
- **Stop conditions:** 2 fix cycles per gate, then save `wip/<bead>` and stop (code reason) or stop for a re-cut (cut reason); docs/style-only red lands with a `residual:` note in the ledger. Two consecutive stops on different beads = the cut is wrong: stop, re-slice before any more implementation. Environment failure (Docker daemon down, registry pulls failing after 3 retries, fixture build failing for a non-code reason, test DB down, quota wall) = stop and report, never loop. Any fence crossing = stop. Model or rails change mid-run = stop after the current bead.

## 5. Gate commands (exact; every agent uses these in a fresh shell)

`P` below is this prefix, run first in every shell:

    source ~/.nvm/nvm.sh && nvm use 20 >/dev/null && cd /Volumes/Dock/Home/Code/photo-wall/.claude/worktrees/photo-wall-readiness-8f8ca1 && export TMPDIR=/Volumes/Dock/tmp UV_CACHE_DIR=/Volumes/Dock/tmp/uv-cache && mkdir -p $TMPDIR

| Gate | Command (after `P &&`) | When |
|---|---|---|
| G-static | `.venv/bin/python -m ruff check . && .venv/bin/lint-imports && python3 scripts/check_docs.py` | every bead |
| G-unit | `PHOTO_WALL_TEST_REQUIRE_DATABASE=1 .venv/bin/python -m pytest -q -m "not db and not browser" -n 4 --dist worksteal --basetemp /Volumes/Dock/tmp/pytest-<bead> <bead's test paths>` | every bead (paths in its page) |
| G-release | `.venv/bin/python -m pytest -q tests/test_release_plan.py tests/test_module_closure.py tests/test_package_closures.py --basetemp /Volumes/Dock/tmp/pytest-<bead>-release` | any bead touching `scripts/`, `appliance/systemd/`, sysusers, or adding/moving a module |
| G-harness | `.venv/bin/python scripts/run_display_harness.py` | B0 onward, any bead touching `appliance/display_host` |
| G-leg | `REV=$(git rev-parse HEAD) && OUT=/Volumes/Dock/tmp/pw-node/$REV && rm -rf $OUT && mkdir -p $OUT && SOURCE_DATE_EPOCH=$(python3 scripts/debian_packages.py epoch) .venv/bin/python -m scripts.build_node_components --repository "$PWD" --revision "$REV" --output $OUT/components && .venv/bin/python -m scripts.build_node_pid1_fixture --components $OUT/components --output $OUT/fixture && PHOTO_WALL_NODE_PID1_FIXTURE=$OUT/fixture PHOTO_WALL_TEST_REQUIRE_NODE_PID1=1 .venv/bin/python scripts/test_local.py -q -m node_pid1 -k "<scenarios>" --basetemp /Volumes/Dock/tmp/node-pid1-<bead> tests/test_node_pid1.py` | beads marked "leg"; builds from the **committed** HEAD (the WIP commit, §7) |

All commands run **unpiped** (no `tee`); paste the tail into the verifier report.

**Milestone gate (once, after D1):** G-static; full unit `PHOTO_WALL_TEST_REQUIRE_DATABASE=1 .venv/bin/python -m pytest -q -m "not db and not browser" -n 4 --dist worksteal --basetemp /Volumes/Dock/tmp/pytest-m1`; DB `docker compose -f tests/integration/compose.test-database.yml up -d --wait && .venv/bin/python scripts/test_local.py -q -m db -n 4 --dist loadgroup --basetemp /Volumes/Dock/tmp/pytest-m1-db`; browser `(cd central/console && npm ci && npm run build) && PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q tests/browser -n 4 --browser chromium`; G-harness; G-leg with `-k "success or failure or outage or reboot or refused or unresponsive"`; mutation probe (a) (B12 page); push and a green CI pipeline on the PR (`checks`, `e2e`, `node-pid1` including the `display-harness` job and the `unresponsive` leg). Counts must be at least the baseline plus the new tests; report skips explicitly.

## 6. Land loop

| Step | Who | Rule |
|---|---|---|
| Implement | implementer (one at a time in this checkout) | reads its bead page + the named design sections only; runs its G-unit/G-static; appends `.claude/errata.md` on a spec contradiction and STOPS; reports net line delta, reuse considered, errata appended, **and where the page is wrong** |
| WIP commit | orchestrator | `git add -A && git commit -m "wip(<bead>): <title>"` before any verifier probe or leg (legs build from a commit; probes restore by `cp` from a backup, never `git checkout` on a dirty tree) |
| Verify | verifier | the bead's gates, once, unpiped; **mutation probe per acceptance criterion** (the page names at least one; each must turn the named test red, then be restored by `cp`); conformance to the frozen page; both branches of new conditionals driven |
| Review | 0–1 lens, high-risk beads only (marked) | findings need file:line or a probe; non-code findings cannot fail the bead |
| Land | orchestrator | fold fix-cycle changes and amend the WIP commit into one Conventional Commit (`git add -A && git commit --amend -m "<type>(<scope>): <summary>" -m "<body>" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"`), update the ledger row (sha), push `claude/player-health-tracer`. Open a **draft PR** after B0 lands (body ends with the attribution line from the session) and enable PR monitoring per the owner's standing rule. Check CI for bead N before bead N+2 starts; a red is triaged as a gate failure of bead N |
| On failure | orchestrator | after 2 fix cycles: `git branch -f wip/<bead> && git push -u origin wip/<bead>`, ledger row `blocked` with findings and branch, reset the working branch to the last landed sha, STOP |

**Architect course-correction pass** after every 5 implementer beads (after B2c, after B7, and pass 3 = M1 coherence after B12 + D1): reads the ledger, errata and diffs; may re-cut a later bead's page (errata entry + new page), never a landed one. A frame-changing gap pauses the run.

## 7. Ledger

| Bead | Status | SHA | Notes |
|---|---|---|---|
| B0 display harness | landed | b380b07 | verified PASS; errata E-B0-1..5 |
| B1 fixture binds a Frame; `unresponsive` healthy | open | | |
| B2a kernel move | open | | |
| B2b feed primitive | open | | |
| B2c lint contracts + ratchet | open | | |
| Architect pass 1 | open | | |
| B3 guest contract + Player responder | open | | |
| B4 shell health layer + fallback tint | open | | |
| B5 Python overlay client parity | open | | |
| B6 probe channel + probe thread + broker feed | open | | |
| B7 app-link accepted locally + outbox + relink | open | | |
| Architect pass 2 | open | | |
| B8 kill after K (Q1 predicate) | open | | |
| B9 catalogue + judge core | open | | |
| B10 display feed for pw-health + overlay op | open | | |
| B11 overlay health drawing | open | | |
| B12 starve role; leg complete | open | | |
| D1 docs | open | | |
| Architect pass 3 / M1 coherence + milestone gate | open | | |

## 8. Ordered beads

Common to every bead: run the gates in its page with the prefix `P`; never edit outside the listed files without an errata entry; a module added or moved under `appliance/` is added to `scripts/release_plan.py` `node-base-deb` paths (and `node-manager-deb` if in the Manager closure) so CI schedules node-pid1 (tests/test_release_plan.py:557-562, :588-597 enforce it); a new unit goes into `scripts/build_node_base_deb.py` `UNITS`, a new launcher into `POLICIES`, a new user or group into its sysusers text (:72-74), a new runtime directory into its tmpfiles text (:75-78).

---

### B0 — Display harness: CI job and Docker local runner
**Packages:** tests, scripts (+ `.github`). **Risk:** CI. **Size:** 6 h. **Leg:** no.
**Frozen page.**
- `scripts/run_display_harness.py` (new, host-side, stdlib only): builds and caches an image `photo-wall-display-harness:<digest>` `FROM scripts.node_build_inputs.BUILDER_IMAGE` (`--platform linux/arm64`), apt sources from `scripts.debian_packages.PIN.sources()`, packages `packages("node-display", "node-display-build")` plus harness-only `python3-pywayland python3-cairo` (digest = sha256 of image recipe text). Runs the container with `appliance/display_host` mounted read-only at `/current-source` and `tests` at `/smoke`, command `python3 /smoke/native_display_smoke.py`; exit code is the harness result; on failure prints `/tmp/pw-weston.log`. CLI: `--keep-image`, `--rebuild`. No network inside the run step.
- `tests/native_display_smoke.py` (promoted, same path): Weston launched with `--debug` in addition to today's flags; adds a pixel helper using `weston_capture_v1` through pywayland bindings generated in-container from libweston-14's `weston-output-capture.xml`; adds `python3 -m pywayland.scanner` over `/current-source/native/photo-wall-frame-v1.xml` and imports the generated `pw_diagnostic_manager_v1` (proves A3: record `pywayland.__version__`). Existing assertions unchanged.
- Assertions (today's behaviour): slate `diagnostic_presented` for the Output; pixel at (40, 40) equals the slate colour (10, 15, 23) ±2; a foreign client binding `pw_diagnostic_manager_v1` gets `private_diagnostic_role` (shell.c:426-429); after handoff a pixel inside the probe app's buffer equals the app colour; trial `overlay_presented` with `frame_tag` `trial-<64 b>` (existing).
- CI: job `display-harness` in `.github/workflows/node-pid1.yml` (`runs-on: ubuntu-24.04-arm`; checkout, `./.github/actions/python-uv`, `uv sync --frozen`, `.venv/bin/python scripts/run_display_harness.py`). `scripts/release_plan.py`: `NOT_SHIPPED += "scripts/run_display_harness.py"`; node-pid1 `Suite.paths += ("tests/native_display_smoke.py", "tests/native_display_probe.c", "scripts/run_display_harness.py")`.
- Also: copy the two design files into `docs/design/player-health/` (see header) — docs-only, `check_docs` must pass.
**Files:** `scripts/run_display_harness.py`, `tests/native_display_smoke.py`, `tests/native_display_probe.c` (only if the probe needs a known fill colour), `.github/workflows/node-pid1.yml`, `scripts/release_plan.py`, `docs/design/player-health/*`.
**Acceptance:** G-harness green locally; `tests/test_release_plan.py` green; the job appears in the PR's node-pid1 run.
**Mutation probes:** change the expected slate colour constant → harness red; make the shell's `bind_diagnostic` skip its private-client check (in a scratch copy mounted instead of `appliance/display_host`) → harness red.
**Test:** G-static, G-release, G-harness.

### B1 — node-pid1 fixture binds a Frame; leg `unresponsive` (healthy form)
**Packages:** tests (+ `.github`). **Risk:** CI. **Size:** 5 h. **Leg:** yes (`unresponsive`, `success`). Pays the cold fixture build.
**Frozen page.**
- `tests/node_pid1_central_fixture.py`: new authenticated endpoint `POST /fixture/bind` (body `{}`) that creates one Frame and binds it to the enrolled Player's connected Output `Virtual-1` through the Registry / operator API the console uses (no raw `INSERT` into `bindings`, `frames` or `installations`); returns `{"player_id", "frame_id", "output_id", "binding_generation", "configuration_revision"}`; 409 `fixture_player_not_enrolled` before enrollment. `/fixture/stage` keeps `fixture_requires_unbound_player` (:293-296) — bound scenarios never stage.
- `tests/test_node_pid1.py`: `SCENARIOS += ("unresponsive",)`; `test_node_pid1_unresponsive`: boot (normal cold), wait for Player enrollment, `POST /fixture/bind`, then wait (≤ 120 s) until the display controller reports the Output admitted (shell `handoff` event on the display feed read as root inside the container, and Central's latest `node_display_exchanges` decision for the Output is the admitting one), then assert fresh app presentations for 20 s. On timeout the failure names the last display decision reason (e.g. `no_current_binding`, `app_process_link_changed`). Evidence: journal, display feed dump, Central decisions.
- `.github/workflows/node-pid1.yml`: matrix `scenario += unresponsive`.
- If the Player presents nothing on a bound Output without a Scene, the fixture authors a minimal Scene through the same operator API (test code only). If admission needs a `central/` change: STOP (fence 3).
**Files:** `tests/node_pid1_central_fixture.py`, `tests/test_node_pid1.py`, `.github/workflows/node-pid1.yml`.
**Acceptance:** G-leg `-k "unresponsive or success"` green; other legs untouched.
**Mutation probe:** skip the `/fixture/bind` call → `unresponsive` red with reason `no_current_binding`.
**Test:** G-static, G-leg.

### B2a — Shared kernel move
**Packages:** appliance, scripts (+ test importers). **Risk:** none. **Size:** 3 h. **Leg:** yes (`success`).
**Frozen page.** Pure moves, no behaviour change, **no re-export shims**:
- `appliance/node/clock.py` → `appliance/clock.py` (`boot_id`, `boottime_ms` unchanged).
- `appliance/node/storage.py` → `appliance/boot_store.py` (`BootStore` unchanged).
- `appliance/node/session.py` → `appliance/central_session/session.py` (`NodeSession`, `REFUSED` unchanged); `appliance/node/http.py` → `appliance/central_session/http.py` (`NodeHTTP`); `appliance/central_session/__init__.py` empty.
- Update every importer (`grep -rn 'appliance\.node\.\(clock\|storage\|session\|http\)\b\|appliance/node/\(clock\|storage\|session\|http\)\.py'` outside `.venv`): 18 under `appliance/` and 9 under `tests/` at 860465c, plus any string reference in `scripts/`.
- `scripts/release_plan.py`: `node-manager-deb` paths replace `appliance/node/{clock,http,session,storage}.py` (:269-270) with `"appliance/clock.py", "appliance/boot_store.py", "appliance/central_session/**"`; `node-base-deb` paths (:276-280) add the same three.
- Docs that cite the old paths are D1's, not this bead's.
**Files:** `appliance/clock.py`, `appliance/boot_store.py`, `appliance/central_session/{__init__,session,http}.py` (moved), every importer the grep lists, `scripts/release_plan.py`.
**Acceptance:** lint-imports still 7 kept; closure and release tests green; the `success` leg green.
**Mutation probe:** drop `"appliance/central_session/**"` from `node-base-deb` paths → `tests/test_release_plan.py::test_every_node_deb_closure_file_is_claimed_by_its_package` red.
**Test:** G-static; G-unit over `tests/test_node_*.py tests/test_release_plan.py tests/test_module_closure.py tests/test_package_closures.py`; G-release; G-leg `-k success`.

### B2b — Feed primitive; display link adopts it
**Packages:** appliance (kernel), display_host. **Risk:** none. **Size:** 3 h. **Leg:** yes (`unresponsive`).
**Frozen page.**
- `appliance/feed.py` (new, stdlib only, thread-safe): `FeedEvent(sequence: int, kind: str, value: dict, audience: Literal["node", "central"])`; `FeedPage(publisher_incarnation: UUID, events: tuple[FeedEvent, ...], gap: bool, latest: int, dropped_total: int)`; `class Feed(capacity: int, *, incarnation: UUID | None = None)` with `append(kind: str, value: dict, *, audience: str = "node") -> int` (drop-oldest, counted) and `read(after: int, *, incarnation: UUID | None, limit: int = 8) -> FeedPage`. **Gap is per read, never sticky:** `gap = (incarnation is not None and incarnation != self.incarnation) or after < oldest_retained - 1`; on an incarnation mismatch the page starts at the oldest retained event. `class FeedCursor` (reader): `request() -> dict`, `advance(page_document: dict) -> tuple[tuple[FeedEvent, ...], bool]` (bool = resnapshot needed). `answer_feed_read(feed: Feed, request: dict) -> dict` (the one wire handler for every publisher). Bounds: `after` int ≥ 0, `incarnation` UUID string or null, `limit` 1..8; else `ValueError("feed_read_request")`.
- Wire, `events` op: request `{"op": "events", "after": int, "incarnation": str | null}`; response keeps `events`, `stream_gap` (now per read), `boot_id`, `incarnation_id` (compositor incarnation, unchanged), adds `publisher_incarnation`. Each event keeps `sequence`, `kind`, `value`.
- `appliance/display_host/runner.py` `Controller` (:75-118): replace the deque, `sequence` and sticky `gap` with one `Feed(256)`.
- `scripts/release_plan.py` `node-base-deb` paths add `"appliance/feed.py"`.
**Files:** `appliance/feed.py`, `appliance/display_host/runner.py`, `scripts/release_plan.py`, `tests/test_feed.py`, display runner tests.
**Acceptance:** a reader that fell behind and caught up gets `gap` false on its next read; a reader holding an old incarnation gets `gap` true once; drops counted.
**Mutation probes:** make `gap` sticky (set on overflow, never reset) → feed test red; ignore the incarnation in the gap rule → restart test red.
**Test:** G-static; G-unit `tests/test_feed.py tests/test_node_display*.py`; G-harness; G-release; G-leg `-k unresponsive`.

### B2c — Lint contracts with an explicit ratchet
**Packages:** pyproject, tests. **Risk:** none. **Size:** 1.5 h. **Leg:** no.
**Frozen page.** Add to `pyproject.toml` `[tool.importlinter]` (verified on a scratch tree with B2a's moves: all kept; upward import, display→node import, health→session each break):
- `layers` "Node contexts point down": `["(appliance.authority)", "(appliance.health)", "appliance.display_host | appliance.node"]`.
- `forbidden` "Display never reads the fault catalogue": `appliance.display_host` → `contracts.node_faults`.
- `forbidden` "Central session only in Authority and the two exemptions": sources `appliance.display_host`, `appliance.node` → `appliance.central_session`; `ignore_imports` exactly: permanent `appliance.node.host_runner -> appliance.central_session.*`, `appliance.node.manager_desired -> appliance.central_session.*`; **ratchet** (removed in M2) `appliance.display_host.service -> appliance.central_session.*` (M2-13), `appliance.node.broker_runner -> appliance.central_session.*`, `appliance.node.app_link -> appliance.central_session.*`, `appliance.node.online_broker -> appliance.central_session.*` (M2-15). Each line carries its comment.
- `forbidden` "Shared node kernel knows no context": sources `appliance.clock`, `appliance.boot_store`, `appliance.feed`, `appliance.central_session`, `appliance.unix_credentials` → `appliance.authority`, `appliance.health`, `appliance.display_host`, `appliance.node`.
- `tests/test_import_contracts.py` (new): parses `pyproject.toml`; asserts the session contract's `ignore_imports` is a subset of the six frozen lines (a new line fails: the ratchet only shrinks) and the layers list equals the frozen list.
**Files:** `pyproject.toml`, `tests/test_import_contracts.py`.
**Acceptance:** lint-imports 11 kept.
**Mutation probes:** add `import appliance.node.broker` to `appliance/display_host/domain.py` → lint red; add a seventh ignore line → ratchet test red; delete the `host_runner` exemption → lint red.
**Test:** G-static; G-unit `tests/test_import_contracts.py`.

### Architect pass 1

---

### B3 — Guest contract (additive) and Player probe responder
**Packages:** contracts, player. **Risk:** public wire, 1 lens (correctness). **Size:** 5 h. **Leg:** yes (`success`, `unresponsive`).
**Frozen page.**
- `contracts/node_app_link.py`: `NODE_APP_LINK_RESULTS = ("accepted", "recorded", "refused")`; `encode_node_app_link_result(status: str) -> bytes`, `parse_node_app_link_result(raw: bytes) -> str` (`ValueError("app_link_result_invalid")` on anything else); probe channel messages, all `{"schema": 2, "kind": ...}`, bounded by `MAX_NODE_LINK_BYTES`: `probe_open` (Player → broker, no other field), `probe` (`nonce`: 64 lowercase hex), `probe_answer` (`nonce`), `relink` (no other field). Functions: `encode_node_probe_open()`, `parse_node_probe_open(raw) -> None`, `encode_node_probe(nonce)`, `encode_node_probe_answer(nonce)`, `parse_node_probe_answer(raw) -> str`, `encode_node_relink()`, `parse_node_probe_channel_message(raw) -> NodeProbeV2 | NodeRelinkV2` (frozen dataclasses; unknown kind → `ValueError("probe_channel_message")`).
- `player/node_app_link.py`: the result is parsed with `parse_node_app_link_result`; `recorded` and `accepted` both return `"recorded"` from `exchange_applied` (callers at player/service.py:1219-1239 unchanged); `refused` returns `"rejected"`.
- `player/probe_responder.py` (new): `ProbeResponder(dispatcher: Callable[[Callable[[], object]], Future], *, on_relink: Callable[[], None], path: Path = DEFAULT_NODE_LINK_SOCKET, connector=_root_socket, sleep=time.sleep)`; `start()` starts daemon thread `player-probe`. Thread: connect (root peer check), **send `probe_open` first**, then receive. On `probe`: under a lock set `pending = nonce`; if no callback is queued, queue one via `dispatcher(self._answer)`; if the returned future already failed with `dispatch_capacity`, clear `pending` and `queued` (no answer). **At most one queued callback; a newer nonce overwrites `pending`.** `_answer` (runs on the GLib main thread, at the dispatcher's default-idle priority): take and clear `pending`, send `probe_answer` with `MSG_DONTWAIT` (drop on `EAGAIN`). On `relink`: call `on_relink()`. On EOF, refusal (`result refused`) or error: close and retry after 0.5, 1, 2, 4, 5, 5 … s (**cap 5 s**, below S).
- `player/service.py`: `PlayerService.request_relink()` (thread-safe; the proof loop (:1163-1243) clears `recorded_node` and `recorded_applied` on its next turn); in `main()` after `service.start()` (:1487): `ProbeResponder(<the same GLibDispatcher instance passed to PlayerService>, on_relink=service.request_relink).start()` — outside `run()`.
- macOS seam: tests inject a fake `glib` into `GLibDispatcher` (it already takes `glib`, :331-333) and a fake connector; no `SOCK_SEQPACKET` needed.
**Files:** `contracts/node_app_link.py`, `player/node_app_link.py`, `player/probe_responder.py`, `player/service.py`, `tests/test_player_probe_responder.py`, app-link and contract tests.
**Acceptance:** idle-starved stub: with a fake glib whose idle queue never runs, no answer is sent and exactly one callback is queued however many probes arrive; un-starved, only the latest nonce is answered, once; capacity refusal → no answer; `accepted` counts as linked; `relink` clears the recorded state; today's broker reply `refused` to `probe_open` → retry with backoff, Player otherwise unaffected (`success` leg green).
**Mutation probes:** answer directly from the responder thread → starved test red; queue one callback per nonce → single-slot test red; treat `accepted` as rejected → app-link test red.
**Test:** G-static; G-unit `tests/test_player_probe_responder.py tests/test_node_app_link*.py tests/test_player_service.py tests/test_contracts.py`; G-leg `-k "success or unresponsive"`.

### B4 — Shell health layer and fallback tint
**Packages:** display_host, tests. **Risk:** security lens (private role). **Size:** 5 h. **Leg:** yes (`unresponsive`).
**Frozen page.**
- `appliance/display_host/native/photo-wall-frame-v1.xml`: `pw_diagnostic_manager_v1` version 2 → 3; request `get_health_layer(id: new_id<pw_health_layer_v1>, surface: object<wl_surface>, output: string)` `since="3"`; new interface `pw_health_layer_v1` version 1 with only `destroy` (destructor). **No argument carries a verdict, code or deadline.** `pw_frame_manager_v1` untouched; `plugin_abi` stays `frame-v3`.
- `shell.c` (≈75 lines): `bind_diagnostic` caps at version 3 (:431); the health layer is a `weston_layer` above every other layer the shell owns (above the protected slate layer, :937-940); a health surface maps when it has a buffer and unmaps on a NULL buffer; handoff (:730), `invalidate()` and the slate path never touch it; one health surface per Output, a second → protocol error `health_layer_exists`; only the private client can reach it (the manager is bound only by `s->diagnostic_client`, :426-429). **Fallback tint:** a per-Output curtain on its own handle (not `o->curtain`), in the health layer, RGBA (0.55, 0.35, 0.0, 0.5), raised whenever the Output is released (handed off) and `s->diagnostic_resource == NULL`; dropped when the private client binds the manager; destroyed in `output_destroyed` (:780-787) and at shell teardown.
- `tests/display_harness_health_client.py` (new, pywayland, harness-only): installed over the spawn path inside the harness container; binds v3, takes a health layer per Output, draws a known tint colour.
- `tests/native_display_smoke.py` adds: (1) after handoff the probe pixel is the app colour blended with the health tint (health layer above app); (2) a v2-bound private client calling `get_health_layer` → protocol error; foreign bind still refused; (3) kill the private client after handoff → fallback tint pixels within 1 s (before the 2 s respawn, :832-836), gone after the respawned client binds; (4) handoff and `invalidate()` leave the health surface mapped; (5) trial `overlay` events unchanged.
**Files:** `appliance/display_host/native/photo-wall-frame-v1.xml`, `appliance/display_host/native/shell.c`, `tests/display_harness_health_client.py`, `tests/native_display_smoke.py`, `scripts/run_display_harness.py` (install the test client), `scripts/release_plan.py` (node-pid1 suite paths += the test client).
**Acceptance:** all five harness steps; production behaviour unchanged except the tint during a private-client respawn.
**Mutation probes:** (b) put the health layer below the app layer → step 1 red; (k) skip raising the fallback tint → step 3 red; drop the private-client check in `bind_diagnostic` → foreign-bind assertion red.
**Test:** G-static; G-harness; G-leg `-k unresponsive`.

### B5 — Python overlay client at parity
**Packages:** display_host, scripts. **Risk:** regression lens (C client deleted). **Size:** 6 h. **Leg:** yes (`success`, `unresponsive`).
**Frozen page.**
- `appliance/display_host/overlay/` (new; package-relative imports only, so it runs in-repo as `appliance.display_host.overlay` and installed as `overlay`):
  - `instruction.py` (pure, Display's published language): `OverlayInstruction(output: str, serial: int, tint: bool, lines: tuple[str, str])`, `PresentedReport(output: str, serial: int)`, `encode_*`/`parse_*` JSON (each line ≤ 96 chars, serial ≥ 0, output ≤ 96 chars); constants `PULSE_DEADLINE_MS = 3000` (D), `INSTRUCTION_STALE_MS = 15000` (V), `UNAVAILABLE_LINES = ("Health status unavailable", "")`. Consumed from B9.
  - `render.py` (pure, no cairo): `render_slate(name, width, height, reason, testing) -> DrawList`, `render_trial(width, height, points) -> DrawList`; `REASON_TEXT` identical to diagnostic-client.c:75-81 including the default; background RGBA (0.04, 0.06, 0.09, α) with α = 0.0 for trial overlay, **0.96 when testing**, 1.0 otherwise (:112); text and geometry as :115-140. `DrawList` = tuple of plain ops (paint, rect, line, arc, text).
  - `paint.py`: `paint(draw_list, surface)` executing ops with cairo (imported lazily).
  - `client.py`: pywayland adapter: connect from `WAYLAND_SOCKET` only (exit 2 without it, as :197); bind `wl_compositor`, `wl_shm`, `wp_presentation`, `pw_diagnostic_manager_v1` **v2**; on `output` configure → slate; on `overlay` configure → trial; before each commit request `wp_presentation` feedback and send `ack(surface, serial)` (so the shell emits `diagnostic_presented` / `overlay_presented`, shell.c:479-491, parsed at weston.py:330-342); caps as the C client (≤ 2 buffers per Output, total ≤ 128 MB, ≤ 8192² px). At start write `900` to `/proc/self/oom_score_adj` (unprivileged raise; failure logged, not fatal).
- `appliance/display_host/meson.build`: remove the `diagnostic-client` executable and the cairo dependency; generate pywayland bindings at build (`python3 -m pywayland.scanner`) for `photo-wall-frame-v1.xml` and `presentation-time.xml` into `overlay/protocol/`; install `overlay/` under `libdir/photo-wall-display/overlay`; install the launcher `diagnostic-client` (Python, `#!/usr/bin/python3 -IB`, mode 0755) at `libdir/photo-wall-display/diagnostic-client` — **the spawn path is unchanged; no C change**.
- Delete `appliance/display_host/native/diagnostic-client.c`.
- `scripts/debian_packages.py`: `python3-pywayland` (node-display, node-display-build), `python3-cairo` (node-display); `libcairo2-dev` removed from node-display-build if nothing else uses it. B0's harness extras then come from these sets.
**Files:** `appliance/display_host/overlay/{__init__,instruction,render,paint,client}.py`, launcher `appliance/display_host/overlay/diagnostic-client`, `appliance/display_host/meson.build`, delete `appliance/display_host/native/diagnostic-client.c`, `scripts/debian_packages.py`, `scripts/run_display_harness.py`, `tests/test_display_overlay_render.py`, `tests/test_display_overlay_instruction.py`.
**Acceptance:** B0 harness unchanged and green with the Python client (`diagnostic_presented`, `overlay_presented`, slate pixel, testing alpha 0.96 blend); macOS unit tests over `render.py` and `instruction.py` without cairo or pywayland installed.
**Mutation probes:** skip `ack` → harness `diagnostic_presented` red; testing alpha 1.0 → unit and harness red; change one `REASON_TEXT` string → unit red.
**Test:** G-static; G-unit `tests/test_display_overlay_*.py`; G-harness; G-release; G-leg `-k "success or unresponsive"`.

### B6 — Probe channel, probe thread, broker feed
**Packages:** node, scripts. **Risk:** none (regression legs). **Size:** 6 h. **Leg:** yes (`success`, `unresponsive`).
**Frozen page.**
- `appliance/node/probe.py` (pure): constants `PROBE_PERIOD_MS = 2000` (T), `MISS_LIMIT = 5` (k), `STARTUP_BUDGET_MS = 20000` (S), `KILL_AFTER_MS = 35000` (K); `AppRunKey(invocation_id: UUID, pid: int, start_ticks: int, app_epoch: int)` from a `RunningApp`; `ProbeClock(run: AppRunKey, started_ms: int)` with `sent(nonce, now_ms)`, `answered(nonce, now_ms) -> bool` (stale nonce → False), `turn(now_ms, *, late: bool) -> tuple[ProbeFact, ...]`. Rules: unanswered time counts from `started_ms` (launch first seen, or broker start) or the last answer; an interval in which the thread's own turn ran more than T/2 late is not counted; no `unanswered` fact before `started_ms + S`; after k counted misses, `probe_unanswered` every T with `unanswered_ms`; `probe_kill_due` once counted unanswered time ≥ K.
- `appliance/node/probe_channel.py`: `ProbeThread(feed: Feed, *, clock=boottime_ms)`, own `selectors.DefaultSelector` and deadline timer; `adopt(connection, run: AppRunKey)`; `publish_run(run: AppRunKey | None, recovery_may_be_armed: bool)` (main loop, each turn); `take_kill_due() -> AppRunKey | None` (main loop); `send_relink(run)` (B7). The thread never calls systemctl, HTTP, the store or the driver. A channel whose run no longer matches the published run is closed.
- `appliance/node/app_link.py` `serve_one`: read the first packet; `kind == "begin"` → today's `handle` path unchanged; `kind == "probe_open"` → admit iff `uid == 10004` and `driver.current()` is not None and its pid equals the peer pid (**no grant check**), then hand the socket to the probe thread (not closed by `serve_one`); anything else → today's refusal.
- `appliance/node/broker_runner.py`: one `Feed(512)`; start `ProbeThread`; each turn publish the current run (one `driver.current()` per turn, reusing the call at :106 when a grant exists, calling it otherwise) and `recovery_may_be_armed` (B8 supplies the predicate; until then `False` and no kill consumer); serve `/run/photo-wall-app-feed/feed.sock` (SOCK_SEQPACKET, 0660 root:`pw-node-feeds`) with `answer_feed_read`, peer allowlist **{0, 10006}**, one request per accept, non-blocking within the turn. Probe facts are `audience="node"`; **no Central-bound code reads the feed**.
- Feed kinds (audience node): `probe_channel` {run, state: open | closed}, `probe_answered` {run, rtt_ms}, `probe_unanswered` {run, unanswered_ms, misses}, `probe_kill_due` {run, unanswered_ms}.
- `scripts/build_node_base_deb.py`: sysusers add `u pw-health 10006 "Photo Wall health judge" /nonexistent`, `g pw-node-feeds 10007`, `m pw-health pw-node-feeds`; tmpfiles add `d /run/photo-wall-app-feed 0750 root pw-node-feeds -`; `stage_tree` adds the symmetric check: the `app-broker` closure contains no `appliance.node.host*` module (`ValueError("app_import_boundary")`). `appliance/systemd/photo-wall-app-broker.service`: `ReadWritePaths += /run/photo-wall-app-feed`.
**Files:** `appliance/node/probe.py`, `appliance/node/probe_channel.py`, `appliance/node/app_link.py`, `appliance/node/broker_runner.py`, `appliance/systemd/photo-wall-app-broker.service`, `scripts/build_node_base_deb.py`, `tests/test_node_probe.py`, `tests/test_node_probe_channel.py`, broker runner tests.
**Acceptance:** healthy channel answered every T; stale nonce ignored; late turns not counted; startup budget honoured; no grant needed to open; a probe never appears in any `session.request` payload (fake session records all requests); in the `unresponsive` leg, `probe_answered` facts readable as root from the broker feed.
**Mutation probes:** (e) count any packet as an answer → stale-nonce test red; count late intervals → late-guard test red; (m) append probe facts to `process-evidence` → Central-payload test red; require a grant for `probe_open` → no-grant admission test red; import `appliance.node.host` in the broker closure → closure test red.
**Test:** G-static; G-unit `tests/test_node_probe*.py tests/test_node_online_broker.py tests/test_node_stop_operation.py tests/test_package_closures.py`; G-release; G-leg `-k "success or unresponsive"`.

### B7 — App-link accepted locally; outbox slot; relink
**Packages:** node. **Risk:** security lens (proof semantics). **Size:** 4 h. **Leg:** yes (`success`, `outage`, `unresponsive`).
**Frozen page.**
- `BrokerLinkService.handle` (app_link.py:104-144): after the existing checks, **keep the `local-app-control` write** (:135-139); write the outbox slot `app-link-outbox` = `{"run": AppRunKey document, "player_id": begin.player_id, "link": encoded app link}` (one slot; a newer proof replaces it); append feed `app_link_accepted` {run, player_id} (audience node); reply `encode_node_app_link_result("accepted")`. **Remove the Central POST from the proof (:141).**
- `deliver_app_link(store, session, probes: ProbeThread) -> None` (app_link.py), called once per main-loop turn when a grant exists: POST `/v2/node/app-links` with the slot's link; 200 → clear slot, feed `app_link_recorded`; `refused_permanently(status)` (online_broker.py:262-265, reused) → clear slot, feed `app_link_refused` {status}, `probes.send_relink(run)`; anything else (transport error, 408, 429, 5xx, 401/403) → keep the slot. **Never dropped on a transient failure.** A slot whose run is no longer current is cleared.
**Files:** `appliance/node/app_link.py`, `appliance/node/broker_runner.py`, `tests/test_node_app_link_local.py`.
**Acceptance:** with Central blackholed, `accepted` within 100 ms of the signed link; the slot survives transient failures and a broker restart; a permanent refusal sends `relink` to that run's channel; the `outage` leg and the `unresponsive` leg (admission needs Central's link record: central/fleet/node_display.py:166) stay green.
**Mutation probes:** (f) POST to Central before replying, Central blackholed → timing test red; drop the slot on a 503 → durability test red; omit `relink` → refusal test red; remove the `local-app-control` write → `tests/test_node_stop_operation.py` (:413) red.
**Test:** G-static; G-unit `tests/test_node_app_link*.py tests/test_node_stop_operation.py tests/test_node_online_broker.py`; G-leg `-k "success or outage or unresponsive"`.

### Architect pass 2

---

### B8 — Kill after K, behind the Q1 predicate
**Packages:** node. **Risk:** security lens (destructive). **Size:** 4 h. **Leg:** yes (`success`, `unresponsive`).
**Frozen page.**
- `recovery_may_be_armed(record: dict | None, acknowledged: dict | None) -> bool` (pure, `appliance/node/probe.py`): True iff `record` has `"recovery"` and `acknowledged` does not name that obligation's `operation_id`. 
- `OnlineEffectBroker.service` (online_broker.py:167-179): after a successful `self.recovery.advance(obligation, proof["progress"])` (no exception), write `recovery-acknowledged` = `{"operation_id": str(obligation.operation_id)}` if different. **No other change to online_broker.py; :98-108, :183-184, :211 untouched.**
- `SystemdAppProcessDriver.kill(expected: RunningApp) -> bool` (process_linux.py): `os.pidfd_open(pid)`; then require `read_proc_start_ticks(/proc, pid) == expected.process.start_ticks` and `systemctl_show(UNIT)` MainPID and InvocationID match; then `signal.pidfd_send_signal(fd, SIGKILL)`; return False (no signal) on any mismatch. `KillMode=control-group` stops the rest; `Restart=no` keeps it down (M1 has no restart).
- `broker_runner` main loop: publish `recovery_may_be_armed(online.broker.record, store.read("recovery-acknowledged"))` each turn; on `take_kill_due()` → kill iff the run equals the current run's key and the predicate is False → feed `app_killed` {run, reason: "unresponsive", unanswered_ms}; else feed `kill_withheld` {run, reason: "recovery_armed" | "run_changed"}.
**Files:** `appliance/node/probe.py`, `appliance/node/online_broker.py` (service only), `appliance/node/process_linux.py`, `appliance/node/broker_runner.py`, `tests/test_node_probe_kill.py`.
**Acceptance:** cold start (no online record) → kill; a switch in flight or an unacknowledged obligation → withheld; identity mismatch → no signal; `success` leg (a switch with recovery) green; `unresponsive` healthy → no kill.
**Mutation probes:** (l) ignore the predicate → recovery-armed test red; skip the start-ticks check → pid-reuse test red; kill before K → timing test red.
**Test:** G-static; G-unit `tests/test_node_probe*.py tests/test_node_online_broker.py tests/test_node_linux_adapters.py`; G-leg `-k "success or unresponsive"`.

### B9 — Catalogue and judge core
**Packages:** health, contracts (+ scripts wiring). **Risk:** none. **Size:** 5 h. **Leg:** yes (`unresponsive`, `success`).
**Frozen page.**
- `contracts/node_faults.py` (new, stdlib only): `Fault(code: str, display_affecting: bool, household_line: str, raise_window_ms: int, clear_hold_ms: int)`; `FAULTS: Mapping[str, Fault]` with one row `app_unresponsive`: True, "Photos paused — the player stopped responding", 5000, 10000; `catalogue_digest() -> str`.
- `appliance/health/judge.py` (pure): `HealthJudge(*, period_ms, miss_limit, startup_ms, kill_after_ms, pulse_deadline_ms, catalogue)`; **construction raises `ValueError("k_rule")` unless K > k·T + raise + D and K > S + raise + D** (shipped: 35000 > 18000 and > 28000). `observe(event: FeedEvent, now_ms)`; `verdict(now_ms) -> Verdict(sequence: int, conditions: tuple[Condition, ...])`; `Condition(code, run, state: "pending" | "raised", age_ms)`. Rules: `probe_unanswered` for the current run → pending; raised after the raise window of continuous unanswered; cleared after `probe_answered` held for the clear hold; `app_killed` keeps the run's condition raised (no restart in M1); unknown code → `ValueError`. Bounded transition ring (256).
- `appliance/health/runner.py`: reads the broker feed via `FeedCursor` every 500 ms (gap → drop probe-derived state for that publisher; never raise from unknown); serves `/run/photo-wall-health/health.sock` (dir 0755, socket 0666) op `status` for **uid 0 only** → verdict + ring. Constants imported from `appliance.node.probe` and `appliance.display_host.overlay.instruction`.
- Unit `appliance/systemd/photo-wall-health.service`: `User=pw-health`, `Group=pw-health`, `SupplementaryGroups=pw-node-feeds`, `RuntimeDirectory=photo-wall-health`, `RuntimeDirectoryMode=0755`, `RestrictAddressFamilies=AF_UNIX`, `ProtectSystem=strict`, `PrivateDevices=yes`, `NoNewPrivileges=yes`, `CapabilityBoundingSet=`, `MemoryMax=64M`, `Restart=always`, `RestartSec=2`, `Slice=photowallbase.slice`, `ConditionKernelCommandLine=photowall.node=v2`, `After=photo-wall-app-broker.service`, `ExecStart=/usr/bin/python3 -I -B /usr/lib/photo-wall-health`. Add it to `photo-wall-node.target` `Wants=` and to the unit lists in `tests/node_pid1_central_inner.py` (:43, :130).
- `scripts/build_node_base_deb.py`: `POLICIES["health-judge"] = ClosurePolicy("health-judge", ("appliance.health.runner",), ("player", "central", "media", "gi"), {})`; `UNITS += "photo-wall-health.service"`. `scripts/release_plan.py` `node-base-deb` paths add `"appliance/health/**"`. `pyproject.toml`: layers `"(appliance.health)"` → `"appliance.health"`; session contract sources add `appliance.health`; `tests/test_import_contracts.py` frozen layers list updated.
**Files:** `contracts/node_faults.py`, `appliance/health/{__init__,judge,runner}.py` (`runner` keeps `if __name__ == "__main__"`; the staged `__main__.py` is generated by scripts/module_closure.py:294-296), `appliance/systemd/photo-wall-health.service`, `appliance/systemd/photo-wall-node.target`, `tests/node_pid1_central_inner.py`, `scripts/build_node_base_deb.py`, `scripts/release_plan.py`, `pyproject.toml`, `tests/test_import_contracts.py`, `tests/test_health_judge.py`, `tests/test_health_runner.py`.
**Acceptance:** the K-rule CI test builds the judge from the shipped constants; rules as above; in the leg the unit is active, `status` shows no condition while healthy, and `systemctl show -p RestrictAddressFamilies photo-wall-health` is `AF_UNIX`.
**Mutation probes:** (d) set K to 25000 → construction test red; raise with no window → rules test red; clear while still unanswered → rules test red.
**Test:** G-static; G-unit `tests/test_health_*.py tests/test_import_contracts.py`; G-release; G-leg `-k "unresponsive or success"`.

### B10 — Display feed for pw-health; per-Output verdict; overlay op
**Packages:** display_host, health (+ scripts wiring). **Risk:** security lens (new allowlists). **Size:** 4 h. **Leg:** yes (`unresponsive`).
**Frozen page.**
- `appliance/display_host/runner.py`: a second listener `/run/photo-wall-display-feed/feed.sock` (SOCK_SEQPACKET, 0660, group `pw-node-feeds`), op `events` only via `answer_feed_read`, peer allowlist **{0, 10006}**; the root-only `ingress.sock` (:180-223) unchanged. `photo-wall-display-controller.service`: `SupplementaryGroups=pw-node-feeds`, `ReadWritePaths += /run/photo-wall-display-feed`. `scripts/build_node_base_deb.py`: tmpfiles `d /run/photo-wall-display-feed 0750 pw-display pw-node-feeds -`; sysusers `m pw-display pw-node-feeds`.
- Judge: reads the display feed (outputs, admissions, invalidations with reason, presentations); `Verdict` gains `outputs: tuple[OutputVerdict, ...]`, `OutputVerdict(output, underlay: "live" | "held" | "slate", codes: tuple[str, ...])` (held = admitted run is the unresponsive run); projection to `OverlayInstruction` per connected Output: tint on iff a display-affecting raised code; line 1 = the household line; line 2 = `"{code} · Player {player_id} · Output {output}"` (player_id from `app_link_accepted`); serial bumps on any change.
- `health.sock` op `overlay` for **uid 10005 only**: the connection stays open; the judge pushes every Output's instruction on change and every 5 s (V/3); receives `PresentedReport` and records `presented` {output, serial} in the ring.
**Files:** `appliance/display_host/runner.py`, `appliance/systemd/photo-wall-display-controller.service`, `appliance/health/{judge,runner}.py`, `scripts/build_node_base_deb.py`, `tests/test_health_*.py`, display runner tests.
**Acceptance:** only uids 0 and 10006 read the display feed; only 10005 opens `overlay`; instructions re-pushed on reconnect; per-Output projection correct for live, held and slate.
**Mutation probes:** allowlist any uid → peer test red; no serial bump on a tint change → projection test red.
**Test:** G-static; G-unit `tests/test_health_*.py tests/test_node_display*.py`; G-harness; G-release; G-leg `-k unresponsive`.

### B11 — Overlay client draws the health layer
**Packages:** display_host, tests. **Risk:** none. **Size:** 5 h. **Leg:** yes (`unresponsive`, `success`).
**Frozen page.**
- `client.py` binds `pw_diagnostic_manager_v1` **v3**; per Output `get_health_layer`; connects to `PHOTO_WALL_HEALTH_SOCKET` (default `/run/photo-wall-health/health.sock`), op `overlay`, reconnecting every 1 s.
- `render.py` adds `render_health(width, height, instruction | None, stale: bool) -> DrawList`: tint = full-Output RGBA (0.0, 0.0, 0.0, 0.45) (single-pixel buffer scaled by `wp_viewporter` when bound, else a full ARGB buffer within the cap); card = opaque rectangle (0.04, 0.06, 0.09, 1.0) bottom-centre with the two lines; tint off → NULL buffer. **Stale:** no instruction for an Output within V (15 s) of client start or of the last instruction → tint + `UNAVAILABLE_LINES`.
- Repaint on each new serial: commit with `wp_presentation` feedback; on `presented` send `PresentedReport(output, serial)`; **`discarded` never reported**; nothing presented within D → log and count (watchdog is M3).
- Harness: a test instruction feeder (fake judge socket in the container, via `PHOTO_WALL_HEALTH_SOCKET`) asserts: tint + card pixels above an admitted app when tint on; NULL buffer when off; presented serial reported; stale card after V of silence.
**Files:** `appliance/display_host/overlay/{client,render}.py`, `tests/native_display_smoke.py`, `tests/display_harness_judge_feeder.py`, `tests/test_display_overlay_render.py`, `scripts/release_plan.py` (node-pid1 suite paths += the feeder).
**Acceptance:** harness steps; no tint on a healthy wall in the `unresponsive` leg (judge publishes tint off) and a `presented` entry in the judge ring.
**Mutation probes:** (c) report on `discarded` → harness red (feeder forces a discard by committing twice before a repaint); draw the tint on the slate surface instead of the health layer → harness "above app" red.
**Test:** G-static; G-unit `tests/test_display_overlay_*.py`; G-harness; G-leg `-k "unresponsive or success"`.

### B12 — Starve role; leg `unresponsive` complete
**Packages:** tests, scripts (+ `.github`). **Risk:** none. **Size:** 6 h. **Leg:** yes (`unresponsive`); the mutation variant runs at the milestone gate.
**Frozen page.**
- `scripts/build_node_pid1_fixture.py`: `ROLES += ("starve",)`; `derive_target` for `starve` moves the Player's `__main__.py` to `__player_main__.py` and writes a wrapper `__main__.py` that installs `GLib.unix_signal_add(GLib.PRIORITY_HIGH, SIGUSR1, start)` where `start` adds `GLib.timeout_add(1, spin, priority=150)` (`spin` busy-loops 20 ms and returns True; GLib priority 150 runs ahead of default-idle (200), where the control dispatch and the probe answer wait, and behind redraw (120), so rendering continues), then runs the original main. Fixture-only; never production code.
- `tests/node_pid1_central_fixture.py`: `central_fixture(..., cold: str = "cold")` selects `deployments[cold]` at :183.
- `tests/test_node_pid1.py` `test_node_pid1_unresponsive`: cold = `starve` (extra ref from the fixture's targets). Steps: (1) bind and admit (B1); (2) healthy for 20 s: `probe_answered` facts, judge `status` has no condition, judge ring shows a `presented` tint-off serial; (3) `systemctl kill --signal=SIGUSR1 photo-wall-node-player.service`; within k·T + raise window + 5 s (20 s) of the signal: judge `app_unresponsive` raised, instruction tint on, ring `presented` for that serial, **and the display feed shows the admission still held (no invalidation) with fresh app presentations**; (4) within K + 10 s of the first miss: broker feed `app_killed`, display feed invalidation `surface_lease_or_process_lost`, slate `diagnostic_presented`, and the instruction still tint on with its serial presented. Evidence: all three feeds, judge ring, journal.
- `.github/workflows/node-pid1.yml` matrix already has `unresponsive` (B1).
- **Mutation probe (a), run by the verifier at the milestone gate:** a local-only `starve_tothread` role whose wrapper replaces `ProbeResponder._answer` dispatch with a direct send from the responder thread; the leg must fail at step 3 (no `app_unresponsive`). Built with a temporary edit to `ROLES`/`derive_target`, restored by `cp`; never committed.
**Files:** `scripts/build_node_pid1_fixture.py`, `tests/node_pid1_central_fixture.py`, `tests/test_node_pid1.py`.
**Acceptance:** leg green locally and in CI.
**Mutation probes:** (a) above; skip the kill on the main loop (scratch revert of B8's consumer) → step 4 red.
**Test:** G-static; G-release; G-leg `-k unresponsive`.

### D1 — Docs
**Packages:** docs. **Size:** 2 h. Not gated on code findings.
`docs/display-host-backend.md` (health layer role, fallback tint, Python overlay client, display feed socket); `docs/module-player-service.md` (:13, probe guest contract, `accepted`, `relink`); `docs/player-node-domain-model.md` (broker accepts proofs locally; probe; kill; judge unit and pw-health; feeds); `docs/requirements.md` (:29, :37: R10 for display-affecting faults, overlay over live content); `docs/execution-contract.md` (:148); `docs/module-appliance-ci.md` (display-harness job, `unresponsive` leg); paths moved in B2a wherever docs cite them; `docs/design/player-health/` history line "M1 delivered". If PR 44's `docs/player-architecture.md` has merged, add an as-built section; otherwise note it in the handoff.
**Test:** `python3 scripts/check_docs.py`.

### Architect pass 3 = M1 coherence, then the milestone gate (§5), then the handoff.

## 9. Handoff (a PR comment on the run's draft PR; also at every stop)

    ## Player health M1 — run report (<date>, <planned end | stopped at gate: …>)
    **Landed:** <bead> <sha> — <one line> (one row each)
    **Stopped:** <bead> — branch `wip/<bead>` — findings: <file:line / failing test excerpt>
    **Open errata:** <ids from .claude/errata.md, one line each>
    **Gates:** milestone gate <green | not reached>; CI run <url>; skips reported: <list>
    **Budget:** tokens <used> / 9.5 M; wall-clock <h> / 26 h; agents max per bead <n>
    **Where the plan was wrong:** <implementer and verifier findings that changed a page>
    **Next ready bead:** <id> on <branch>
    **Start here:** <one paragraph for the next session: first bead, its branch, its page, what to read>

Then commit the updated ledger (§7) and push; leave no ledger row `in_progress`.
