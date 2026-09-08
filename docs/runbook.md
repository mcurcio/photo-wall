# Development setup and recovery

Status: central, PostgreSQL, and the Procrastinate media worker launch together. Earlier two-Player/three-Output and native evidence established the MVP shape. Final committed-revision evidence remains required for the complete demo, authenticated browser walkthrough, current exact-image native paths, and automatic central rollback; physical qualification also remains outstanding. Follow the [delivery checklist](implementation-checklist.md) and [evidence](evidence/README.md); do not treat earlier local-state evidence as acceptance of the current design.

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

The operator interface lists Players and Outputs, creates persistent Frames, binds equipment, retires a Player, and previews/commits/reverts calibration. It also creates immutable Sources and Scenes, schedules Programs, starts/finishes/cancels Runs, and shows source/worker health. Program timestamps use the browser's displayed local time zone. Configure the private upstream connection on the worker before creating a Source with its connection ID. The disposable browser walkthrough below covers these controls; final-revision delivery evidence remains separate. With the scheduler enabled, `/healthz` is green only when the database is reachable and a scheduler tick completed successfully within the last 10 monotonic seconds; `starting`, `coordination_unavailable`, `stale`, and `stopped` states return 503 with fixed sanitized status fields. Explicit test mode can disable the scheduler and retain database-only health semantics. A green `/healthz` reports service liveness, not observed presentation.

The worker starts with an empty private connection list and remains healthy while idle. Its configuration is described in [the worker module](module-media-worker.md); a deployment must provision that file as UID 10001, mode 0600 in the `connections` volume and restart `worker`. Never put an upstream API key in operator forms, Source definitions, Player configuration, Git or command-line arguments. The [Immich fixture](module-immich-fixture.md) generates its own synthetic media and disposable private configuration for reproducible adapter tests.

To provision a real worker connection, use the Compose service's mounted
`connections` volume and feed a separately prepared private file through
Docker standard input. This keeps the API key out of shell arguments, history
and Docker output. Prepare the source file outside the repository with the
deployment's approved secret editor or secret manager, and make it mode 0600
before using it:

```sh
python3 - <<'PY'
import os
from pathlib import Path
path = Path.home() / ".photo-wall-connections.json"
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.fchmod(fd, 0o600)
os.close(fd)
print("Created private connection file with mode 0600.")
PY
```

The command refuses to overwrite an existing file. Do not place that file in
Git or a shared temporary directory; populate it with the deployment's approved secret
editor or secret manager, keep it for the next command only, and remove it
with the deployment's secret-management procedure afterward.

Use this shape as the file contents, replacing only the values in the approved
private editor. `base_url` must end in `/api` (or `/api/`), and `ca_file` is a path
inside the worker container when a separately provisioned private CA is
required:

```json
{"schema":1,"connections":[{"connection_id":"immich-main","base_url":"https://immich.example.test/api","owner_id":"00000000-0000-4000-8000-000000000000","api_key":"PASTE_KEY_HERE","allow_http":false,"ca_file":null}]}
```

Validate and publish the staged file as the worker UID. The existing worker
loader performs the schema, ownership, mode, size, URL/TLS-policy and
secret-shape checks; it does not replace an actual upstream TLS connection
test. A failed check removes the staged file without printing its contents.

```sh
docker compose run --rm --no-deps -T --user 10001:10001 \
  --entrypoint /bin/sh worker -c '
set -eu
trap "rm -f /etc/photo-wall/private/.connections.json.new" EXIT
umask 077
cat > /etc/photo-wall/private/.connections.json.new
chmod 0600 /etc/photo-wall/private/.connections.json.new
python -c "from pathlib import Path; from media.worker import load_connections; load_connections(Path(\"/etc/photo-wall/private/.connections.json.new\"))" >/dev/null 2>&1
mv /etc/photo-wall/private/.connections.json.new /etc/photo-wall/private/connections.json
' < ~/.photo-wall-connections.json
docker compose restart worker
docker compose exec -T worker stat -c '%u:%g %a' /etc/photo-wall/private/connections.json
```

The final command must report `10001:10001 600`. Updating this file takes
effect after a worker restart. Do not use `cat` to display the private document,
an inline heredoc, or a command argument for the private document or API key.
Worker diagnostics are sanitized, but never send the private document to a log
command. The fixture helpers remain disposable test configuration and do not
provision a deployment worker.

In the Scene form, keep live source selection for a changing collection, or
select **Choose a photo or video for each Frame** to keep a specific current
asset for each participating Frame. Each chooser lists only media compatible
with that Frame. The Scene and its references save together; source freshness,
membership and compatibility are rechecked centrally. Playback still requires
prepared media.
The [authored-media contract](module-authored-media.md) describes retention and
capacity. Development checks for this form require Node.js and execute its
event flow with a synthetic DOM; this is separate from the authenticated
browser walkthrough.

For immediate activation, choose a priority and whether an already active Scene
should be ignored, restarted, or queued. Queue requests require an expiry;
**Force** is an explicit override. Queued and ignored requests report their
actual outcome without claiming that a new Run started.

`PHOTO_WALL_HORIZON_SECONDS` defaults to 300 seconds. In the scheduler's PostgreSQL transaction, the media repository records each bounded preparation request and defers its exact job ID through Procrastinate. The separate worker publishes verified derivatives into the `media` volume. Central mounts it read-only and serves exact authorized bytes; Players never receive an upstream URL or credential. Procrastinate owns dispatch, retry timing, and queue-worker liveness. Photo Wall schedules only domain preparation, source refresh, and publication/storage maintenance tasks; it retains publication recovery, reservations, and stale-attempt fencing. The worker has a read-only runtime, a private writable media volume, and container CPU/memory limits.

Boot selection exists at `/v1/bootstrap/boot`, root images are served from `/appliance/rootfs-<sha256>.squashfs`, and fresh key-proof enrollment uses `/v1/enrollment/challenge` plus `/v1/enrollment/register`. The single-process Player entry point is `python -m player.service --config /etc/photo-wall/public.json`; see [service configuration and runtime requirements](module-player-service.md). The Player reads its RAM boot context, creates a new process key, and receives a new central authority epoch. A recognized returning equipment observation restores central bindings; unknown equipment remains unbound. `/v1/player/time` supplies independent authenticated clock samples and `/v1/player/boot-health` binds release health to the current ticket/session. Physical Pi/PXE and complete current-image qualification remain pending. Startup-only DRM discovery currently requires a Player restart after connector topology changes.

The `database` volume persists all authoritative state, including Procrastinate jobs and release/trial records. `media` holds central authoritative media blobs and preparation files, and `connections` holds private worker configuration. Central and worker startup apply forward Photo Wall SQL migrations under a database advisory lock and check hashes of already-applied migrations; they also install/upgrade Procrastinate's versioned schema. Do not edit an applied Photo Wall migration; add another numbered migration. Both containers run as an unprivileged user. The exact Python dependency graph is in `uv.lock`.

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

The [real-browser registry walkthrough](../tests/browser/test_operator_browser.py) and [content walkthrough](../tests/browser/test_operator_content_browser.py) use the production operator HTML, JavaScript, and HTTP API against their own temporary PostgreSQL schemas. Install the locked development dependencies and their matching Chromium build, then run:

```sh
uv sync --frozen
.venv/bin/python -m playwright install chromium
PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q tests/browser \
  --browser chromium --tracing retain-on-failure --output artifacts/operator-browser
```

CI installs Chromium's Linux dependencies with `playwright install --with-deps chromium` and runs this explicitly; ordinary test runs skip browser checks unless opted in. Playwright 1.62.0 and pytest-playwright 0.9.0 are pinned in the development dependency group and `uv.lock`; neither enters production services or the Player package. See the official [pytest runner](https://playwright.dev/python/docs/intro) and [CI setup](https://playwright.dev/python/docs/ci-intro).

The registry walkthrough proves rejected/accepted authentication, reconnection after a rejected token, rejection of delayed failures from an earlier login attempt even when the same token is reused, two-Player/three-Output inventory, Frame creation/binding, calibration preview/revert/commit, stale-tab conflict recovery, replacement/retirement, and persistence through a fresh server/connection pool. It checks preview expiry using controlled time. The content walkthrough creates a Source, saves live and per-Frame authored Scenes, checks compatible prepared-photo choices and current selection guidance, schedules and removes Programs using the browser's local time zone, and starts, ignores, queues, naturally finishes, and cancels Runs. Definitions, Programs, and Run history survive a fresh server/connection pool.

All operator mutations use browser controls, and every page rejects uncaught JavaScript errors. The fixtures supply simulated equipment and generated public JPEGs through production source-refresh, acquisition-request, and publication transactions with an explicitly synthetic recipe and preparation metadata. Elapsed time advances through the production Runtime owner; natural finish waits for the current cycle boundary. The server binds an ephemeral loopback port, preserving the separate full demo's network isolation. These checks qualify operator controls and persistence. They do not run an upstream adapter, conversion worker, background scheduler, Player, or renderer, and do not qualify scheduled playback, PXE, or physical output. Run browser checks separately from the ordinary suite: synchronous Playwright owns an event loop for its session, while ordinary Player integration tests create their own loops.

The bounded schema-2 `operator-browser.json` report records named assertions, pass/failure status, browser version, PostgreSQL/fixture scope, generated-media and controlled-time inputs, checkout revision, dirty state, and GitHub event/SHA. CI always uploads available reports and retains traces only for failed tests. Pull-request runs identify the synthetic merge checkout; dispatching `MVP checks` on the PR branch records the dispatched commit instead. A dirty local run is diagnostic evidence, not final committed-revision acceptance. Reports and failure traces contain only the disposable fixture's public test token and synthetic records; keep unrelated deployment data out of the fixture.

## Recovery

```sh
docker compose restart central worker
docker compose logs --tail 100 central worker database
docker compose up -d --build --wait
```

Restart preserves the database volume. `docker compose down` stops this deployment without removing the volume. Back up PostgreSQL using `pg_dump` before migration or deployment changes; restoring production backups has not yet been qualified. Never use `down --volumes` on a deployment whose registry must be retained.

If an Output moves, bind the destination persistent Frame. Returning recognized equipment automatically receives its centrally stored binding after fresh enrollment. For replacement equipment, explicitly change the binding from the old equipment Output to the new registered Output; observations alone never transfer operator intent. Frame geometry survives, generation increases, and playback requires revalidated calibration. Starting or reconnecting a Player rotates session credentials/authority without creating another Frame or changing desired geometry.

Preview carries a 30-second expiry and both proposed/committed settings in current process memory so the Executor can revert during a running-process outage. Commit and revert use optimistic revision and binding-generation checks. A stale browser must refresh before retrying. Partitioned equipment respects the bounded plan lease and rejects obsolete work when it obtains fresh session authority. Cold reboot requires central time/release/enrollment/control/media connectivity. A surviving cache file can avoid a media request only after the new process validates it against the current assignment; it cannot restore authority.

The [real Immich fixture](module-immich-fixture.md), [full media-path demo](module-wall-demo.md), [Player-only package builder](module-player-package.md), and [central release contract](module-appliance-release.md) provide commands and evidence boundaries. The [appliance builder/bootstrap](module-appliance-builder.md), [GitHub ARM image workflow](module-appliance-ci.md), and [headless image e2e gate](module-appliance-e2e.md) describe exact-artifact checks and their limits. Earlier signed image and hosted boot evidence remains useful for artifact identity and generic-VM behavior, but its durable-Player/local-update assumptions are superseded. Complete current-image native rendering, valid-cache reuse, corrupt-cache reacquisition, real automatic reboot/central rollback, and physical measurements remain pending until recorded against the final revision.
