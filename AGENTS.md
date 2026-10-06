# Agent guidance

Photo Wall drives a wall of photo/video Frames. A **Central** service (FastAPI + PostgreSQL) and its **media worker** own content, planning and fleet control; **Player** nodes netboot from Central, are Immich-unaware, and get media only from Central.

This page is the map; open an owning document only when the routing table sends you there.

## Code map

| Path | Owns | Entry points |
|---|---|---|
| `central/` | Registry, Runtime, Planner, operator API; content-serving layers (below) | `app.py:create_app` (Compose), `node_app.py:create_app` (V2 node transport) |
| `central/fleet/` | Node boot offers, sessions, commands, rollout, recovery | `routes.py`, `node_routes.py`, `service.py` |
| `central/console/` | React/Vite operator console, served at `/` | `src/App.jsx`; `npm ci && npm run build` here → `dist/` |
| `central/console/src/` route tables | Sections by bounded context: `showRoutes.jsx`, `wallRoutes.jsx`, `fleetRoutes.jsx` (Players list + one Player page per box, [console DDD](docs/operator-console-ddd.md)), `neutralRoutes.jsx` | `Shell.jsx`; fleet: `PlayersPage.jsx`, `PlayerPage.jsx`, `facts.js` |
| `central/migrations/` | Forward-only numbered SQL; never edit an applied one | applied at Central/worker startup |
| `media/` | Media worker, Immich adapter, preparation | `python -m media.worker` |
| `player/` | Single-process Player (GTK/GStreamer), cache, executor | `service.py` |
| `contracts/` | Versioned wire models shared by every side | `models.py`, `node_*.py` |
| `uplink/` | Stdlib-only initramfs client that locates and trusts Central | `locate.py`, `fetch.py` |
| `appliance/` | OS base, netboot initramfs, provisioning, systemd units, OS agent | `netboot_init.py`, `os_agent.py`, `systemd/` |
| `appliance/kernel/` | Node kernel: primitives on the stdlib, `contracts` and `uplink` alone (clock, boot store, credentials, capacity, boot-stage records, probe timing) | `capacity.py`, `boot_stage.py` |
| `appliance/host/` | HostCore: host metrics and facts, operator reboot, local recovery deadlines | `host_runner.py` |
| `appliance/boot/` | The Node's one-shot boot stages: storage, handoff, prepare | `node_bootstrap.py` |
| `appliance/apps/` | App lifecycle: effect broker, progress probe, app process, stop, root import | `broker_runner.py`, `online_runner.py`, `root_import.py` |
| `appliance/health/` | Health judge: Node facts to one verdict and the overlay instruction | `runner.py`, `judge.py` |
| `appliance/node/` | *Retiring:* AppManager, preparer, app link, recovery; gains no new code | `manager_runner.py` |
| `appliance/central_session/` | *Retiring:* the Node's Central session and HTTP, imported only by the listed workers | `session.py` |
| `appliance/display_host/` | Weston display host and native C clients | `runner.py`, `native/` |
| `scripts/` | Builds, fixtures, demos, wrappers | `test_local.py`, `demo_wall.py`, `immich_fixture.py`, `check_docs.py` |
| `tests/` | `test_*.py` (unit + DB); `node/<ctx>/` Node tests per context (`apps`, `boot`, `display`, `health`, `host`; cross-context ones in `node/`), imported by path under `tests/` (`node.apps.test_x`), so a new directory needs no registration and may reuse file names; `browser/` Playwright; `integration/` Compose files; `test_node_pid1.py` + `node_pid1_*` real-systemd node scenarios | `conftest.py` assigns tiers |
| `.github/workflows/` | `pipeline.yml` gates merges; calls `checks.yml`, `software-e2e.yml`, `netboot-e2e.yml`, `node-pid1.yml`, … | |

**Import layering** (`pyproject.toml` `[tool.importlinter]`): `contracts` imports no domain or persistence package; `uplink` is stdlib-only (no pydantic, httpx or domain package); `player` never imports `central`, `media`, `appliance` or a database/queue; Central layers run `app : content_wiring : content_routes` → `infra` → `content_catalog | assets | health` → `origins` → `kernel`, the inner ones free of psycopg, Procrastinate and FastAPI. Node contexts under `appliance/` run `node` (retiring) → `boot | netboot_init` → stage-1 helpers → `apps` → `health` → `display_host | host` → `central_session` (retiring) → `kernel | feed` (feed retiring), each importing only the layers below and never a `|` sibling; `boot` is an island that reaches no other context; `host` reaches no sibling context; only `nodeapi` imports NATS.

## Verify

| Tier | Command | Authority |
|---|---|---|
| Static | `.venv/bin/python -m ruff check .` · `.venv/bin/lint-imports` · `python3 scripts/check_docs.py` | CI + local |
| Unit | `PHOTO_WALL_TEST_REQUIRE_DATABASE=1 .venv/bin/python -m pytest -q -m "not db and not browser" -n 4 --dist worksteal` | CI; Linux-only tests skip on macOS |
| DB | `docker compose -f tests/integration/compose.test-database.yml up -d --wait`, then `.venv/bin/python scripts/test_local.py -q -m db -n 4 --dist loadgroup` | CI + local |
| Browser | `PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q tests/browser -n 4 --browser chromium` (test database, built console, `playwright install chromium`) | CI (pinned Playwright container) |
| Images, Linux media | `image-smoke`, `linux-media` jobs in `checks.yml` | CI only |
| Wall e2e | `scripts/demo_wall.py` ([recipe](docs/module-wall-demo.md#reproducing-the-current-checkpoint)); refuses uncommitted changes to `central/`, `media/`, `contracts/`, `player/`, `Dockerfile`, `pyproject.toml`, `uv.lock` | CI (`software-e2e.yml`) |
| PID1 node scenarios | `-m node_pid1` with `PHOTO_WALL_NODE_PID1_FIXTURE` from `scripts/build_node_pid1_fixture.py`; arm64 Docker, privileged ([guide](docs/evidence/player-node-handoff-support/node-lifecycle-qualification.md)) | CI (`node-pid1.yml`, one leg per scenario) + local |
| Physical Pi, PXE, HDMI, timing | bench evidence ([which evidence](CONTRIBUTING.md#choose-the-right-evidence)) | nothing automated |

Published-wire tests skip unless `PHOTO_WALL_PUBLISHED_PLAYER_WIRE_DIR` names a directory built by `scripts/published_player_wire.py prepare`. Under `CI`, a skip outside `CI_SKIP_ALLOWLIST` fails. Details: [runbook tests](docs/runbook.md#tests-and-local-development).

## Environment quirks

- Put large scratch (pytest `--basetemp`, `UV_CACHE_DIR`, `TMPDIR`, builds) on a roomy volume.
- `uv run` can rewrite `uv.lock`; revert it before committing. Prefer `.venv/bin/python` after `uv sync --frozen`.
- Docker registry pulls can fail intermittently; retry, and let CI be the DB/image gate if they keep failing.
- `scripts/configure.py` creates a private `.env`; never commit it, and never put real credentials in source, fixtures or evidence.

## Touching X → read Y

| Touching | Read |
|---|---|
| Product behaviour, terminology | [requirements](docs/requirements.md) |
| Components, boundaries | [architecture](docs/architecture.md); content serving: [Central system architecture](docs/central-system-architecture.md) |
| Runtime, Planner, execution | [execution contract](docs/execution-contract.md) |
| Player node lifecycle | [node architecture as built](docs/player-architecture.md), [Player node domain model](docs/player-node-domain-model.md), [fleet implementation map](docs/player-fleet-implementation-map.md), [display host](docs/display-host-backend.md) |
| Operator console | [UX design](docs/operator-console-ux-design.md) |
| Launch, deploy, recovery | [runbook](docs/runbook.md#local-launch) |
| A consequential choice | [design decisions](docs/design-decisions.md), [decision records](docs/decisions/) |
| Acceptance, evidence | [validation](docs/validation.md), [evidence conventions](docs/evidence/README.md) |
| Process | [design principles](CONTRIBUTING.md#design-principles), [orchestration](CONTRIBUTING.md#recursive-development-and-agent-orchestration), [subagent models](CONTRIBUTING.md#subagent-model-selection), [credentials](CONTRIBUTING.md#fixtures-and-credentials) |

## Non-negotiables

- **DRY and SOLID take precedence over YAGNI** ([design principles](CONTRIBUTING.md#design-principles)). The top-level agent orchestrates ([orchestration](CONTRIBUTING.md#recursive-development-and-agent-orchestration)).
- Report skipped integration checks explicitly. Never invent successful runs or hardware results.
- Distinguish acquired files, playback readiness, capacity, commitment and observed output; route failures to planning and lifecycle ownership.
- Keep credentials and private media out of source and fixtures. Use relative documentation links. Put each policy in its owning document and link to it.

## Preserve product behavior

- Frames are persistent locations; Players and Panels are replaceable equipment.
- Scenes own sources and presentation configuration. Programs schedule activation; Runs are executions.
- All affected Frames and Actuators participate explicitly. Omitted targets receive no implied command.
- Hard compatibility precedes preferences, including authored assignments. Empty pools do not relax eligibility.
- Live query results evolve; scheduled and secured assignments retain their content for that execution.
- Covered Runs advance logically and reveal current state. Natural completion and downward-only cancellation differ.
- Operational equipment state remains distinct from authored content.
- [Plug-and-play PXE provisioning](docs/requirements.md#player-provisioning) brings a Player into the central system without local setup.
- [Players are Immich-unaware](docs/requirements.md#central-media-boundary) and obtain media exclusively from the central Photo Wall service.
