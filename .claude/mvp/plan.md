# Central MVP delivery plan: OS image + Player `.deb` through the new design (PR #22)

The design is `docs/central-system-architecture.md` (approved). The binding model is `implementation-workflow`.
Each bead's frozen page is the lane file named below. Beads land as one Conventional Commit each. The
orchestrator alone edits `ledger.md`. The light gate per bead is the changed package's unit tests with fakes,
`ruff check .` and `lint-imports`. DB tests are written now and run in CI. Revert `uv.lock` after any `uv run`.

## Phases and order

```
P0.1 → P0.2 ──┬─ Lane A (A-1 → A-2 → A-3 → A-4) ─┐
              ├─ Lane B (B-1 → B-2 → B-3 → B-4) ─┤
              ├─ Lane C (C-1 → C-2 → C-3 → C-4) ─┼─→ P2.1 → P2.2 → P2.3 → push → deferred verification
              └─ Lane D (D-1 [+D-2])            ─┘
```
- **P0 (serial, `P0-kernel.md`).** P0.1 writes the kernel and all package markers, plus ALL import-linter
  contracts. P0.2 writes `central/infra/transactions.py` and `tests/fakes/`. After it lands, every lane codes
  against Protocols and fakes only.
- **P1 (4 lanes, concurrent).** Each lane runs in its own worktree branched from the P0.2 tip, with one
  implementer per worktree. The files are pairwise DISJOINT, so the lanes merge in any order without conflict.
  Merge order A, B, C, D.
- **P2 (serial, `P2-wiring.md`).** The composition roots, the route rewire and the legacy removal. Then push.

## Lane ownership (a lane edits ONLY these paths)

| Lane | Page | Owned paths |
| --- | --- | --- |
| A job runtime | `lane-A-job-runtime.md` | `central/infra/{job_queue,outcomes,execution,runtime,publisher,outcome_feed,queue_ops}.py`, `central/migrations/020_job_outcomes.sql`, `tests/test_infra_{job_queue,execution,runtime_boot,publisher_conformance,outcome_feed,queue_ops}.py`, `tests/runtime_fakes.py` |
| B content catalog | `lane-B-content-catalog.md` | `central/content_catalog/{ports,boot_policy,catalog,sync}.py`, `central/infra/catalog_records.py`, `tests/test_content_catalog_*.py`, `tests/test_infra_catalog_records.py`, `tests/catalog_fakes.py` |
| C assets | `lane-C-assets.md` | `central/assets/{layout,store,os_image,production,handlers,reader}.py`, `central/infra/asset_records.py`, `central/migrations/021_assets.sql`, `tests/test_assets_*.py`, `tests/test_infra_asset_records.py` |
| D origins + health | `lane-D-origins-health.md` | `central/origins/github.py`, `central/health/probe.py`, `tests/test_origins_github.py`, `tests/test_health_probe.py` |

These shared files have exactly one owner:
- `pyproject.toml` contracts: P0.1 writes them all; P2.3 adds only the top-layer siblings.
- `central/*/__init__.py` and `central/infra/transactions.py`: P0.
- `tests/fakes/`: P0.
- `central/app.py`, `media/worker.py`, `central/content_wiring.py`, `central/content_routes.py`,
  migration `022`, and every legacy deletion: P2.
- `uv.lock`: nobody. There are no new dependencies.

## Explicitly NOT in the MVP (each is a later bead or programme)

| Cut | Cost while it is cut |
| --- | --- |
| Media (`PrepareMedia`, media sync jobs, the `media-variant` kind, `/v1/media/{original}/{recipe}`, TRANSCODE queue) | Media stays on its current path, as a legacy procrastinate loop inside the same worker process |
| **`MaintainCache`** (LRU under budget, orphan and temp sweep) | **Deferred: correctness does not need it.** The disk decides presence and no record claims a file. Cost: the cache only grows, and the old tag GC and orphan sweep are deleted in P2. Crash temps (`.tmp-*`) accumulate. The operator can wipe the cache at any time (it is ephemeral). |
| Release withdrawal (retiring tags gone upstream) | Stale references stay desired only if pinned or known-good; otherwise they are inert |
| Fleet health (`WorkerBeat`, `FailingOutcome`, `FleetHealth`) and the "produced but absent" metric | `job_outcomes` already records everything they would read |
| `/v1/operator/netboot` `cache` and `boot_status` sections | Operators lose the base-cache table view |
| `PHOTO_WALL_RELEASE_POLL_SECONDS` override | The cadence is fixed by the job type (15 min) |
| Moving the worker root to `central/worker.py`; dropping legacy tables (`base_cache`, `app_packages`, `app_package_policy`, `base_boot_status`, `mirror_*` columns) | Dead tables remain; migration 021 reads them once, for the backfill |
| The base-health route's move into the catalog | `record_base_health` stays in a pruned `netboot_base.py` |
| Multi-field asset subjects | These arrive with media (a co-change of kernel check 8) |
