# Service image layer and cache checks — 2026-09-28

Status: local simulation and image checks passed; hosted GHA measurements are
pending. The [CI contract](../module-appliance-ci.md#service-image-builds) owns
the layering and cache policy. Every number below is a local measurement on one
amd64 sandbox, not a hosted result.

## The report that prompted it

[Pipeline run 36463535409](https://github.com/mcurcio/photo-wall/actions/runs/36463535409)
(the push of merge `081ecf6` to `main`) showed low BuildKit cache use on every
service image build. Per-step `CACHED` markers in its job logs:

| Job | Target | Cached steps |
|---|---|---|
| checks / portable-and-postgres | media-worker | 7 of 20 |
| checks / linux-media | media-test | 8 of 22 |
| netboot-e2e / tracer | central (arm64) | 7 of 17 |
| e2e / two-players-three-outputs | media-worker (arm64) | 15 of 20 |
| images | central | 16 of 17 |

The expensive dependency steps (`uv sync`, `npm ci`) were already cache hits.
The misses followed from these causes:

- **Ref scoping.** `main` cannot restore a pull request's caches, so the merge
  push rebuilt every layer the pull request changed. The pull request's own last
  run ([36461379453](https://github.com/mcurcio/photo-wall/actions/runs/36461379453))
  also missed 13 of 20 worker steps, because its latest commits changed code.
- **Chained source layers.** `COPY central` preceded the `contracts`, `media`
  and `player` copies and the project install. Any change under `central/`,
  including the console sources, invalidated all of them and every worker step
  after them.
- **The worker re-shipped its environment.** `COPY --from=runtime /app /app` put
  the 64 MB third-party environment and the code in one 69 MB layer, which
  changed with every code change.
- **The test image re-installed its test dependencies.** `media-test` ran
  `uv sync --frozen` after the code, so each code change downloaded the test
  group again and wrote a 204 MB layer.
- **Shared scopes replaced each other.** `photo-wall-checks-amd64-v1` and
  `photo-wall-software-e2e-arm64-v1` each had several writers, and an export
  replaces its scope's index. AMD64 `central` was never exported, and two arm64
  scopes (`photo-wall-central-arm64-v1` and `photo-wall-worker-arm64-v1`) were
  read but never written.

The main cost of a cached build turned out to be transfer, not re-execution. A
fully cached worker build with `--load` still pulled 389 MB from the cache,
mostly the retained media OS.

## Method

Each job ran on a fresh `docker-container` builder (BuildKit v0.32.2), loading
its images like CI. A local cache directory per scope stood in for the GHA
backend. Exports went in place, so blobs the cache already held were skipped
and each scope's index was replaced, as a GHA export does. Both jobs of a commit
imported the cache as it was before that commit, because CI runs them in
parallel. Each layout ran the same commit sequence from a cold cache: a seed
commit, an unchanged commit, a comment added to `central/app.py`, then a
comment added to `central/console/src/App.jsx`.

The old layout used the old scopes: the worker wrote the `checks` scope and
central only read it, and `media-test` read `checks` and `media-test` and wrote
`media-test`. The new layout used one scope per target. Base images came from
the pinned references and the published amd64 media OS
`sha256:dcfa8cdf…`.

The sandbox reaches package indexes through a TLS proxy. A test-only transform,
never committed, added a secret-mounted CA to the network steps of both
layouts; a secret mount does not enter cache keys.

## Results

Durations are wall-clock seconds for one run each, so differences of a few
seconds are within noise. "Cached" is the build summary's step count; "pulled"
is the data transferred from the cache.

| Change | Job | Target | Old: seconds, cached, pulled | New: seconds, cached, pulled |
|---|---|---|---|---|
| none | portable-and-postgres | media-worker | 24.6 s, 100 %, 389 MB | 21.8 s, 100 %, 343 MB |
| none | portable-and-postgres | central | 3.3 s, 94 %, 30 MB | 1.9 s, 93 %, 31 MB |
| none | linux-media | media-test | 31.3 s, 100 %, 515 MB | 35.0 s, 100 %, 500 MB |
| Python | portable-and-postgres | media-worker | 31.3 s, 55 %, 396 MB | 26.9 s, 66 %, 359 MB |
| Python | portable-and-postgres | central | 1.6 s, 94 %, 0 MB | 3.3 s, 81 %, 30 MB |
| Python | linux-media | media-test | 44.2 s, 50 %, 435 MB | 38.7 s, 61 %, 517 MB |
| console | portable-and-postgres | media-worker | 32.4 s, 45 %, 396 MB | 28.4 s, 55 %, 359 MB |
| console | portable-and-postgres | central | 1.6 s, 94 %, 0 MB | 2.8 s, 81 %, 30 MB |
| console | linux-media | media-test | 44.0 s, 40 %, 396 MB | 40.3 s, 52 %, 517 MB |

For a code change, the two jobs' image builds took about 10 % less time in
total, and the worker and test builds cached 10 to 12 more percentage points of
steps. `central` now exports its own scope, which costs it one to two seconds
more. `media-test` now pulls its prebuilt test environment from the cache
rather than downloading it from PyPI.

Images and per-change layers:

| | Old | New |
|---|---|---|
| `central` image | 333 MB | 272 MB |
| `media-worker` image | 959 MB | 905 MB |
| worker layers that change with the code | 69 MB (`/app` with its environment) | 2 MB of source, plus a 2.8 MB project install |
| uv binary in the images | 40.5 MB | none |

The project-install layer is mostly the standard-library bytecode described
below. The old worker copied `/app` from another stage and never received that
bytecode; the new worker runs its own project install, so it now does.

Two findings changed the design during the work:

- **Import only the target's own scope.** Importing several scopes into one
  build made the result depend on their order. Two fresh builders built the
  worker and then `central`, each exporting its own scope. Rebuilding `central`
  with imports ordered `central, worker` missed its three `COPY --link` tail
  steps. Importing `central` alone, or ordering `worker, central`, hit every
  step. The action therefore imports only the target's own scope, with which
  each of the three targets rebuilt fully cached.
- **The project install writes bytecode the runtime relies on.** An attempt to
  keep the build backend's bytecode out of the project-install layer
  (`PYTHONDONTWRITEBYTECODE=1`) made
  `test_cancellation_reaps_active_native_process_and_cleans_destination` fail
  three times out of three. The old image passed three times out of three. The
  pinned Python base ships no standard-library bytecode, and a read-only
  container cannot write any, so the modules the build backend compiles speed
  every later interpreter start. The change was reverted, the reason is
  documented in the Dockerfile, and the test passed three times out of three.

## Other verification

- **Image parity (independent review):** User, WorkingDir, Env, Volume,
  Entrypoint, Cmd and exposed ports match between the old and new images. The
  file trees under `/app`, `/etc/photo-wall` and `/var/cache/photo-wall` match
  apart from the intended removal of uv and its cache, and one directory mode:
  the worker's `/app/central/console` is 0755 rather than the build context's
  mode. The editable `.pth`
  still names `/app`. `central.app` imports with a read-only root filesystem.
- **Full media suite in the final `media-test` image:** 55 passed, run with
  networking disabled, as CI runs it.
- `.venv/bin/python -m pytest -q`: 2411 passed, 833 skipped. The skips are the
  PostgreSQL and host-tool integration tests; `scripts/test_local.py` was not
  run because no Compose database was available.
- `ruff check .` passed.
- [actionlint 1.7.12](https://github.com/rhysd/actionlint/releases/tag/v1.7.12),
  checksum-verified, reported no findings in the changed workflows. It flags
  `queue: max` in the unchanged `service-base.yml` as unknown syntax.

## Limits

- Hosted measurements are pending. Hosted runners and the GHA cache backend
  have different bandwidth from this sandbox, and arm64 was not simulated.
- The new scope names start empty, so the first pull request run and the first
  `main` run after merging rebuild from cold.
- A local compose build of `media-os`, which needs Debian snapshot access, was
  not run.
