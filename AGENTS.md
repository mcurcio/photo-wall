# Agent guidance

Photo Wall drives a wall of photo/video Frames. A **Central** service (FastAPI + PostgreSQL) and its **media worker** own content, planning and fleet control; **Player** nodes netboot from Central, are Immich-unaware, and get media only from Central.

This page is the map; open an owning document only when the routing table sends you there.

## Code map

| Path | Owns | Entry points |
|---|---|---|
| `central/` | Registry, Runtime, Planner, operator API; content-serving layers (below) | `app.py:create_app` (Compose), `node_app.py:create_app` (V2 node transport) |
| `central/fleet/` | Node boot offers, sessions, commands, rollout, recovery | `routes.py`, `node_routes.py`, `service.py` |
| Central's bus side | The worker keeps the hub and records every Node's bus ([0017](docs/decisions/0017-node-redesign-r3.md) C20, [runbook](docs/runbook.md#the-node-bus-and-centrals-hub)): `node_bus_wiring.py` (the worker's one bus task), `fleet/node_bus_hub.py` (FleetHub: hub configuration, reload, WALL, retirement, presence looks), `fleet/node_bus_accounts.py` (the hub configuration), `infra/node_links.py` (one NodeLink supervisor per pipe), `infra/node_link_store.py` (the PostgreSQL link store and WALL mark), `fleet/node_bus_presence.py` (presence tables, read by the Player page), `fleet/leaf_bridge.py` (the leaf's WebSocket relay at Central's origin) | `central/node_bus_wiring.py:build_node_bus` |
| `central/console/` | React/Vite operator console, served at `/`; catalog layers `src/design`, `src/ui`, `src/patterns`, `src/domain`, `src/pages` import only the layers below them ([0018](docs/decisions/0018-console-by-domain-and-design-system.md)) | `src/App.jsx`; `npm ci && npm run build` here → `dist/` |
| `central/console/src/` route tables | Sections by bounded context: `showRoutes.jsx`, `wallRoutes.jsx`, `fleetRoutes.jsx` (Hardware list and one Hardware page per Pi on the design system; one Software and screens page per box until it moves too, [console DDD](docs/operator-console-ddd.md), [0018](docs/decisions/0018-console-by-domain-and-design-system.md)), `neutralRoutes.jsx` | `Shell.jsx`; fleet: `pages/hardware-page.tsx`, `pages/hardware-pi-page.tsx`, `PlayerPage.jsx`, `facts.js` |
| `central/migrations/` | Forward-only numbered SQL; never edit an applied one | applied at Central/worker startup |
| `media/` | Media worker, Immich adapter, preparation | `python -m media.worker` |
| `player/` | Single-process Player (GTK/GStreamer), cache, executor | `service.py` |
| `contracts/` | Versioned wire models shared by every side | `models.py`, `node_*.py` |
| `nodeapi/` | Node API library, the one NATS client ([0017](docs/decisions/0017-node-redesign-r3.md) C4, C5, C13, C18): the Node-role session (attach, event outbox, state with `birth`, methods, desired and wall views); Central's per-pipe NodeLink and WallWriter, with the connection-owning `run_link`, `HubAdmin` and `HubUnavailable` Central's worker runs; the document writer and reader; circular and sticky buffers, a component's `Slice` in its store line, and `apply`; the pull budget and cursor reader; the envelope headers; stream epochs and tokens | `node.py`, `hub.py`, `documents.py`, `buffers.py`, `pull.py`, `envelope.py`, `epoch.py` |
| `uplink/` | Stdlib-only initramfs client that locates and trusts Central | `locate.py`, `fetch.py` |
| `appliance/` | OS base, netboot initramfs, provisioning, systemd units, OS agent | `netboot_init.py`, `os_agent.py`, `systemd/` |
| `appliance/kernel/` | Node kernel: primitives on the stdlib, `contracts` and `uplink` alone (clock, boot store, credentials, capacity and the memory line table, boot-stage records, read-only image mounts through PID1, probe timing) | `capacity.py`, `boot_stage.py`, `image_mount.py` |
| `appliance/host/` | HostCore: host metrics and facts, operator reboot, local recovery deadlines; `birth` on the bus's host line | `host_runner.py`, `bus.py` |
| `appliance/boot/` | The Node's one-shot boot stages: storage, handoff (writes the bus environment), prepare | `node_bootstrap.py`, `bus_environment.py` |
| `appliance/bus/` | The Node bus unit: nats-server conf, systemd unit and its persistent slice ([0017](docs/decisions/0017-node-redesign-r3.md) C19) | `photo-wall-bus.service`, `node-bus.conf` |
| `appliance/apps/` | App lifecycle: effect broker, progress probe, app process, stop, root import; release roots staged as images (`stage_image`, `mounted_root`, the release check) | `broker_runner.py`, `online_runner.py`, `root_import.py`, `environment.py` |
| `appliance/health/` | Health judge: Node facts to one verdict and the overlay instruction | `runner.py`, `judge.py` |
| `appliance/node/` | *Retiring:* AppManager, preparer, app link, recovery; gains no new code | `manager_runner.py` |
| `appliance/central_session/` | *Retiring:* the Node's Central session and HTTP, imported only by the listed workers | `session.py` |
| `appliance/display_host/` | Weston display host and native C clients | `runner.py`, `native/` |
| `scripts/` | Builds, fixtures, demos, wrappers | `test_local.py`, `demo_wall.py`, `immich_fixture.py`, `check_docs.py` |
| `tests/` | `test_*.py` (unit + DB); `node/<ctx>/` Node tests per context (`apps`, `boot`, `display`, `health`, `host`; cross-context ones in `node/`), imported by path under `tests/` (`node.apps.test_x`), so a new directory needs no registration and may reuse file names; `browser/` Playwright; `integration/` Compose files; `test_node_pid1.py` + `node_pid1_*` real-systemd node scenarios | `conftest.py` assigns tiers |
| `.github/workflows/` | `pipeline.yml` gates merges; calls `checks.yml`, `software-e2e.yml`, `netboot-e2e.yml`, `node-pid1.yml`, … | |

**Import layering** (`pyproject.toml` `[tool.importlinter]`): `contracts` imports no domain or persistence package; `uplink` is stdlib-only (no pydantic, httpx or domain package); `player` never imports `central`, `media`, `appliance` or a database/queue; Central layers run `app : content_wiring : content_routes` → `infra` → `content_catalog | assets | health` → `origins` → `kernel`, the inner ones free of psycopg, Procrastinate and FastAPI. Node contexts under `appliance/` run `node` (retiring) → `boot | netboot_init` → stage-1 helpers → `apps` → `health` → `display_host | host` → `central_session` (retiring) → `kernel | feed` (feed retiring), each importing only the layers below and never a `|` sibling; `boot` reaches no other context except the retiring prepare verb's `apps.environment` edge, which E6 removes; `host` reaches no sibling context; only `nodeapi` imports NATS (others reach it through `nodeapi`; `central` uses only `nodeapi.hub`, never `nodeapi.node`), and `nodeapi` imports only `contracts`, NATS and the stdlib, its modules running `node | hub` → `documents` → `buffers | pull | envelope` → `epoch`.

## Verify

| Tier | Command | Authority |
|---|---|---|
| Static | `.venv/bin/python -m ruff check .` · `.venv/bin/lint-imports` · `python3 scripts/check_docs.py` | CI + local |
| Unit | `PHOTO_WALL_TEST_REQUIRE_DATABASE=1 .venv/bin/python -m pytest -q -m "not db and not browser" -n 4 --dist worksteal` | CI; Linux-only tests skip on macOS |
| DB | `docker compose -f tests/integration/compose.test-database.yml up -d --wait`, then `.venv/bin/python scripts/test_local.py -q -m db -n 4 --dist loadgroup` | CI + local |
| Node bus | `PHOTO_WALL_NATS_SERVER=$(python3 scripts/nats_server.py fetch --dest <dir>) .venv/bin/python -m pytest -q tests/integration -k node_bus -n 4`; the DB tier's `test_central_*` need the same variable; the arm64 fence is `test_bus_memory_fence.py` with `PHOTO_WALL_BUS_FENCE_SERVER` | CI (`node-bus`, `db`, `bus-fence`) + local |
| Browser | `PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q tests/browser -n 4 --browser chromium` (test database, built console, `playwright install chromium`) | CI (pinned Playwright container) |
| Console static | `PATH=$HOME/.nvm/versions/node/v22.23.2/bin:$PATH npm --prefix central/console run typecheck` and `run lint` (Node 22; S2, S3 in [0018](docs/decisions/0018-console-by-domain-and-design-system.md)) | CI + local |
| Console catalog | `tests/browser/test_console_catalog_browser.py` walks the Storybook build (render, axe, screenshot diff) | CI (`console-catalog` leg) |
| Images, Linux media | `image-smoke` (also checks the Compose hub: the worker's clients on it and WALL), `linux-media` jobs in `checks.yml` | CI only |
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
| Node bus, hub, NodeLink, `nodeapi` | [0017](docs/decisions/0017-node-redesign-r3.md) (C3–C5, C13, C19, C20), [runbook](docs/runbook.md#the-node-bus-and-centrals-hub) |
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
