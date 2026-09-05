# Development setup and recovery

Status: central registry foundation runs; complete end-to-end MVP, real media playback, appliance image and physical qualification are in progress. Follow the [delivery checklist](implementation-checklist.md) and [evidence](evidence/README.md). Do not deploy this partial development service as a qualified wall controller.

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

The operator interface currently lists Players and Outputs, creates persistent Frames, binds equipment, retires a Player, and previews/commits/reverts calibration. Automatic key-proof enrollment exists at `/v1/enrollment/challenge` and `/v1/enrollment/register`; no runnable appliance client/PXE path is delivered yet. Source/Scene/Program authoring, media worker and live presentation health remain on the checklist. A green `/healthz` reports database connectivity, not observed presentation.

The `database` volume persists PostgreSQL. Central startup applies forward SQL migrations under a database advisory lock and checks hashes of already-applied migrations. Do not edit an applied migration; add another numbered migration. The central container runs as an unprivileged user. The exact Python dependency graph is in `uv.lock`.

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

CI installs the locked dependencies, lints, checks local documentation links, builds/launches the actual Compose services, runs the full PostgreSQL suite, and checks central HTTP health. Passing CI does not establish physical Pi/PXE, real Immich, rendering or visible timing.

A disposable operator fixture is available with `.venv/bin/python -m scripts.demo_registry` after starting the database. It listens on localhost:8010, prints a public fixture token, and registers two simulated Players (two Outputs and one Output) in its own temporary schema. Stop it with Ctrl-C to remove that schema. It is a registry demo only; it does not render or emulate PXE.

## Recovery

```sh
docker compose restart central
docker compose logs --tail 100 central database
docker compose up -d --build --wait
```

Restart preserves the database volume. `docker compose down` stops this deployment without removing the volume. Back up PostgreSQL using `pg_dump` before migration or deployment changes; restoring production backups has not yet been qualified. Never use `down --volumes` on a deployment whose registry must be retained.

If an Output moves, bind the destination persistent Frame. If a Player is replaced, retire its old identity and bind the new registered identity's Outputs to the existing Frames. Frame geometry survives; generation increases and playback requires revalidated calibration. A retired key cannot register again. Token rotation invalidates the old token and authority epoch; it does not create another Frame or change desired geometry.

Preview carries a 30-second expiry and both proposed/committed settings so the future executor can revert locally through an outage. Commit and revert use optimistic revision and binding-generation checks. A stale browser must refresh before retrying. Offline old equipment cannot learn of immediate retirement through a partition; it must reject obsolete work on rejoin and respect the bounded plan lease. Player warm-outage/cold-reboot policy is defined in [decision 0001](decisions/0001-mvp-time-recovery-and-module-contracts.md); its full execution adapter is still in progress.

Image build, PXE, media fixture integration, scoped update/rollback, and physical measurement commands will be added and verified with those implementations. No image checksum or boot claim exists yet.
