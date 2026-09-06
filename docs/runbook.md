# Development setup and recovery

Status: central, PostgreSQL and the media worker launch together. The full real-media wall demo has passed with two network Players and three recording-renderer Outputs; native rendering has separate Linux evidence. Final appliance image, boot and physical qualification remain in progress. Follow the [delivery checklist](implementation-checklist.md) and [evidence](evidence/README.md).

## Local launch

Prerequisites: Git, Docker Engine/Desktop with Compose, and Python 3.12 for development tests. Docker runs the central Python dependencies and PostgreSQL; no paid runtime service is required.

```sh
git clone https://github.com/mcurcio/photo-wall.git
cd photo-wall
git checkout feat/runnable-mvp
python3 scripts/configure.py
docker compose up -d --build --wait
curl --fail http://127.0.0.1:8000/healthz
```

Open `http://127.0.0.1:8000`. Read the operator token from the private `.env` file and enter it in the operator interface. The script creates `.env` with mode 0600 and never overwrites it. No credentials are committed. The development listener and database port bind only to loopback. Appliance deployment requires the separately configured HTTPS/PXE trust boundary; this local listener is not that deployment.

The operator interface lists Players and Outputs, creates persistent Frames, binds equipment, retires a Player, and previews/commits/reverts calibration. It also creates immutable Sources and Scenes, schedules Programs, starts/finishes/cancels Runs, and shows source/worker health. Program timestamps use the browser's displayed local time zone. Configure the private upstream connection on the worker before creating a Source with its connection ID. Full browser walkthrough remains pending; HTTP API workflow tests have passed. A green `/healthz` reports database connectivity, not observed presentation.

The worker starts with an empty private connection list and remains healthy while idle. Its configuration is described in [the worker module](module-media-worker.md); a deployment must provision that file as UID 10001, mode 0600 in the `connections` volume and restart `worker`. Never put an upstream API key in operator forms, Source definitions, Player configuration, Git or command-line arguments. The [Immich fixture](module-immich-fixture.md) generates its own synthetic media and disposable private configuration for reproducible adapter tests.

`PHOTO_WALL_HORIZON_SECONDS` defaults to 300 seconds. The scheduler enqueues bounded preparation requests, and the separate worker publishes verified derivatives into the `media` volume. Central mounts it read-only and serves exact authorized bytes; Players never receive an upstream URL or credential. The worker has a read-only runtime, a private writable media volume, and container CPU/memory limits. Its pinned Linux FFmpeg build is qualified separately from host conversion tools.

Automatic key-proof enrollment exists at `/v1/enrollment/challenge` and `/v1/enrollment/register`. The single-process Player entry point is `python -m player.service --config /etc/photo-wall/public.json`; see [service configuration and runtime requirements](module-player-service.md). The common appliance/PXE path remains under construction. Startup-only DRM discovery currently requires a Player restart after connector topology changes.

The `database` volume persists PostgreSQL, `media` holds the central preparation cache, and `connections` holds private worker configuration. Central and worker startup apply forward SQL migrations under a database advisory lock and check hashes of already-applied migrations. Do not edit an applied migration; add another numbered migration. Both containers run as an unprivileged user. The exact Python dependency graph is in `uv.lock`.

## Tests and local development

Install the free `uv` Python package manager, then:

```sh
uv sync --frozen
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
python3 scripts/check_docs.py
```

The portable command reports PostgreSQL integration tests as **skipped** unless `PHOTO_WALL_TEST_DATABASE_URL` is set. To run all tests against the local Compose database:

```sh
.venv/bin/python scripts/test_local.py -q
```

That wrapper reads local `.env` as data, never sources it as shell code. Each PostgreSQL test creates a random `pw_test_*` schema and removes only that schema. It preserves registry data in the deployment's public schema. A custom integration server may be supplied through `PHOTO_WALL_TEST_DATABASE_URL` with permission to create/drop test schemas. Keep it pointed at a development server.

CI installs the locked dependencies, lints, checks local documentation links, builds/launches Compose, runs the PostgreSQL suite, and checks central HTTP health. It separately runs all preparation tests inside the pinned Linux worker image, so missing host FFmpeg cannot silently remove that gate. Passing CI does not establish physical Pi/PXE, real Immich, rendering or visible timing.

A disposable operator fixture is available with `.venv/bin/python -m scripts.demo_registry` after starting the database. It listens on localhost:8010, prints a public fixture token, and registers two simulated Players (two Outputs and one Output) in its own temporary schema. Stop it with Ctrl-C to remove that schema. It is a registry demo only; it does not render or emulate PXE.

## Recovery

```sh
docker compose restart central worker
docker compose logs --tail 100 central worker database
docker compose up -d --build --wait
```

Restart preserves the database volume. `docker compose down` stops this deployment without removing the volume. Back up PostgreSQL using `pg_dump` before migration or deployment changes; restoring production backups has not yet been qualified. Never use `down --volumes` on a deployment whose registry must be retained.

If an Output moves, bind the destination persistent Frame. If a Player is replaced, retire its old identity and bind the new registered identity's Outputs to the existing Frames. Frame geometry survives; generation increases and playback requires revalidated calibration. A retired key cannot register again. Token rotation invalidates the old token and authority epoch; it does not create another Frame or change desired geometry.

Preview carries a 30-second expiry and both proposed/committed settings so the Executor can revert locally through an outage. Commit and revert use optimistic revision and binding-generation checks. A stale browser must refresh before retrying. Offline old equipment cannot learn of immediate retirement through a partition; it rejects obsolete work on rejoin and respects the bounded plan lease. Player warm-outage/cold-reboot policy is defined in [decision 0001](decisions/0001-mvp-time-recovery-and-module-contracts.md). Reboot reuses the durable key and cached bytes but obtains fresh authority before execution.

The [real Immich fixture](module-immich-fixture.md), [full media-path demo](module-wall-demo.md), [Player-only package builder](module-player-package.md), and [signed update store](module-appliance-release.md) provide their commands and evidence boundaries. The [appliance builder/bootstrap](module-appliance-builder.md) now includes automatic healthy-trial acceptance. Final common-image/PXE and physical measurement instructions remain under qualification. No final appliance checksum or boot claim exists yet.
