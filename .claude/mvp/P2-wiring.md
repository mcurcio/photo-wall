# P2 — wiring, route rewire, legacy removal, push (serial, after lanes A–D have merged)

Design: §2, §6, §8 and §10.5 ("Worker boot … runs JobRuntime"; "`create_app` binds no handlers").
Pages in force: `P0-kernel.md` and `lane-*.md` (all merged). Per-bead gate: the changed
packages' tests, the rewritten HTTP tests, `ruff check .` and `lint-imports`. The DB cases skip locally;
CI is the DB gate. Revert `uv.lock` after any `uv run`.

## P2.1 Composition helper + worker root → `JobRuntime`

**Create `central/content_wiring.py`.** It is the one composition helper, shared by both roots (two named
consumers: `create_app` and `media/worker.py`).
```python
WAITER_SLOTS: Final = 32                                    # §2: per-pod waiter cap for OS/.deb
WORKER_CONCURRENCY: Final[Mapping[QueueName, int]] = {QueueName.FETCH: 2, QueueName.UPKEEP: 2}
@dataclass(frozen=True, slots=True)
class ContentServices:                                      # what the Central process needs
    catalog: ReleaseCatalog
    reader: AssetReader
    probe: PodProbe
    feed: OutcomeFeed | None                                # started/stopped by the lifespan
def build_content_services(db: Database, clock: Clock, *, cache_root: Path) -> ContentServices: ...
def build_job_runtime(db: Database, clock: Clock, *, cache_root: Path,
                      env: Mapping[str, str]) -> JobRuntime: ...
    # every CATALOG handler: SyncReleasesHandler, FetchOsImageHandler, FetchPackageHandler,
    # PrefetchHandler, RescueStalledJobsHandler, PurgeFinishedJobsHandler; publisher with feed=None;
    # origin = GitHubReleaseOrigin.from_env(env)
```

**`media/worker.py` `_entry`.**
- Keep the media path byte-for-byte: `MediaWorker`, `worker_lock()`, `create_worker_app` restricted to `MEDIA_QUEUE`.
- Run the legacy media app and `JobRuntime.run()` side by side in one `asyncio.TaskGroup`.
  Both use `install_signal_handlers=False`.
- Install ONE SIGTERM/SIGINT handler (`loop.add_signal_handler`). It calls `runtime.stop()` and stops the legacy
  worker gracefully; the process exits 0.
- Delete `worker_queues`, the `register_app_release_tasks` call, `boot_autopull` and the autopull task, and the
  `AppReleaseService`/`resolve_base_root` wiring. The inline boot calls `_register_recipe`, `maintain` and
  `refresh_once` STAY, because they are media and media is out of scope.

**Delete** `media/app_release_tasks.py`, `central/app_release_boot.py`, `central/app_release_service.py`,
`tests/test_app_release_tasks.py` and `tests/test_app_release_boot.py`.

**Migration `022_retire_release_queue.sql`.** Guarded by `to_regclass('procrastinate_jobs') IS NOT NULL`,
it sets `status='cancelled'` on `todo` rows in queue `photo-wall-app-release`. Those tasks have no consumer anymore.
CI proves that procrastinate's status trigger accepts the transition.

**Acceptance.**
- A unit test builds `build_job_runtime` over a stub `Database` with `PHOTO_WALL_*` env and gets a
  runtime. That proves the boot checks passed: every CATALOG type has exactly one handler.
- `python -c "import media.worker"` succeeds.

## P2.2 Central read path

**Create `central/content_routes.py`.** It is a composition-root module and may import fastapi.
```python
async def until_disconnect(request: Request, operation: Awaitable[T]) -> T: ...
    # cancels `operation` on http.disconnect (media_gateway.py:42-49 pattern); CancelledError → caller
def mount_content_routes(app: FastAPI, content: ContentServices) -> None: ...
```

**`create_app` signature.**
- REMOVE `release_queue`, `app_root` and `base_root`.
- ADD `content: ContentServices | None = None`. When it is None, `build_content_services(db, clock,
  cache_root=cache_layout.cache_root())` builds it; that happens only for a real `Database`.
- The lifespan runs `db.migrate()` → `apply_schema` → `content.feed.start()`, then `feed.stop()` on exit.
- `create_app` binds no handlers.

| Route | Behaviour | Errors |
| --- | --- | --- |
| `GET /v1/netboot/base` (async, unauth) | `resolve(NetbootBaseRequest(header))`. `Candidates` → `until_disconnect(reader.read)`. On `Opened`: `catalog.record_served(req, opened.job)`, then stream (`application/octet-stream`, `Content-Length`, `Digest: sha-256=<b64>`, `Cache-Control: public, immutable`). Log the sanitized serial as today. | `Unknown` → **404** `{"error":"base_unknown"}` (decision 4). `Unavailable` → **503** `{"error":"base_<reason>"}` + `Retry-After` |
| `GET /v1/app/package/{sha256}.deb` (async, unauth) | a malformed sha → 404. `resolve(PackageRequest)` → read → stream with today's headers | 404 `app_package_not_found`; 503 `app_<reason>` + `Retry-After` |
| `GET /v1/netboot/manifest` | `device_package(serial)` → `{version, sha256, size, tag}` | `ManifestRefusal` → 503 `{"error": code}` |
| `GET /v1/app/manifest` | `promoted_package()` → `{version, sha256, size}` | 503 `{"error": code}` (`app_unconfigured` keeps the appliance retrying) |
| `GET /livez` | 200 `{"status":"ok"}` | — |
| `GET /readyz` | `probe.ready()` → 200 / 503 | — |
| `/healthz` | UNCHANGED: compose, CI and scripts poll it | — |

Stream from the returned fd with the existing 1 MiB generator. The generator's close closes the fd.

**Tests: `tests/test_content_routes_http.py`.** `create_app(db=<stub>, content=<built from
catalog_fakes + InMemoryAssetRecords + RecordingPublisher + CacheStore(tmp_path)>)` covers every row above:
- 404 vs 503.
- `Retry-After` present.
- The Digest matches the bytes.
- `record_served` is called only on a 200.
- A disconnect frees the waiter slot.
- A pinned device is never served another tag.

## P2.3 Operator routes + legacy removal

| Route | New behaviour |
| --- | --- |
| `GET /v1/operator/app/releases` | `releases_view()` |
| `POST /v1/operator/app/releases/{tag}/promote` | `promote(tag)` → 200 `{"status":"promoted"}` (the fetch is published; there is no 202 "pending" branch any more) |
| `POST /v1/operator/app/releases/refresh` | `refresh()` → 202 `{"status":"polling"}` |
| `PUT/DELETE /v1/operator/devices/{id}/pin` | `pin` / `unpin` → `{"status":"pinned"\|"cleared"}` (no inline GC) |
| `GET /v1/operator/netboot` | `{"frontier", "devices"}`; the `cache`, `boot_status` and `base_root_configured` keys are dropped (MVP cut) |
| `POST /v1/operator/app`, `PUT /v1/operator/app/current` | **REMOVED** (owner decision; the design says there is no hand-uploaded `.deb`) |

`CatalogError` maps kinds to status: `not_found` → 404, `conflict` → 409, `invalid` → 422, each with body `{"error": code}`.

**Delete** `central/app_release_queue.py`, `central/app_releases.py`, `central/app_packages.py` and
`central/github_releases.py`. **Prune** `central/netboot_base.py` to `SERIAL_HEADER`, `sanitize_serial`,
`device_id_for_serial` and `record_base_health`: the base-health route stays legacy in the MVP. Remove every
superseded test:
- `test_app_release_http.py`, `test_app_releases.py`, `test_app_package_http.py`, `test_github_releases.py`
- `test_netboot_base.py` (its extraction cases are ported in C4)
- `test_netboot_base_{boot,gc,http,observability,pin,recovery,tracer}.py` (ported in B1, B7 and P2.2)
- `test_netboot_manifest_http.py`

Treat `test_netboot_e2e_wire.py`, `test_netboot_fresh_install_e2e.py` and `scripts/test_netboot_e2e.py`
file by file:
- rewire each to the new `create_app` signature, OR
- delete it and file a `residual:` bead that names the lost behaviour.

Never delete silently. Report each decision.

**Import-linter (the only P2 edit to `pyproject.toml`).** Put the new roots in the top layer:
`layers = ["central.app : central.content_wiring : central.content_routes", ...rest unchanged]`.
`:` makes them non-independent siblings. No new forbidden contracts.

**Acceptance.**
- `grep -rn "app_release\|AppPackages\|github_releases\|enqueue_base_fetch" central media` → only `migrations/`.
- `lint-imports` is green.
- The full local non-DB `pytest -q` is green.

## Push (orchestrator main loop; after P2.3 lands)
1. `git log --oneline` shows one Conventional Commit per bead. The ledger is updated with each sha.
2. `git push origin claude/central-system-architecture`. This updates PR #22.
3. Post a PR comment with the ledger, the MVP cuts (`plan.md`) and the deferred-verification list.

## After the push: deferred verification (not part of this run)
- The full CI (Postgres). The conformance, feed, rescue, retry-effect and repo DB tests run there first.
- Mutation probes per lane acceptance criterion.
- Adversarial code review: security (unauth read routes, tar extraction), correctness (outcomes, the since race),
  and regression (appliance provisioning against the new 503/404 semantics).
- The two-pod shared-disk test (the §9 tracer proof).
- Docs bead: `docs/module-central-cache.md`, `docs/runbook.md`, the README routes, and the removal of
  `POST /v1/operator/app` from the docs.
