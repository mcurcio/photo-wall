# 0013 unified cache root — delivery ledger

Design of record: [0013-unified-cache-root.md](0013-unified-cache-root.md) (v10,
committed 77f481f). Owner requirements: [0013-owner-requirements.md](0013-owner-requirements.md).
Frame approved (owner handed Slice 1 for delivery). Branch: `feat/0013-unified-cache-root`
off `origin/main` (0012 / PR #18 already merged).

Verify gate (from AGENTS.md / 0012 process, binding):
- fast (local): `.venv/bin/python -m ruff check . && .venv/bin/lint-imports && python3 scripts/check_docs.py && env -u PHOTO_WALL_TEST_DATABASE_URL .venv/bin/python -m pytest -q`
- full (CI only): build images → `docker compose up -d --wait` → `.venv/bin/python scripts/test_local.py -q`
- **No local Docker** — DB/compose/e2e is the CI gate; locally DB tests must COLLECT clean, CI executes them.
- **NEVER `uv run`** (mutates uv.lock — revert if touched).
- Baseline (pre-Slice-1, b0f6b0b tree): `992 passed, 328 skipped`.

Budget: per bead 90 min wall / 8 agents; stop-and-report on cap; failed work → pushed `wip/<bead>`.

## Slice 1 — unbreak netboot (tracer first)

| Bead | Intent | Probes | Status | SHA / branch | Notes |
|---|---|---|---|---|---|
| T (tracer) | ONE `PHOTO_WALL_CACHE_ROOT` + internal subdir derivation across 4 readers; release-sourcing + base-serving **always-on** (remove `app_root/base_root is None` gates + `503 release_sourcing_unconfigured`); Dockerfile (ARG PUID/PGID, GID pin, ENV cache-root, `install -d` 3 subdirs before `VOLUME`, `VOLUME` in central+media-worker leaves); entrypoint rewrite (install -d per subdir, drop per-domain env, keep numeric zero-uid guard, wire into central stage); compose remount at cache root; in-slice test updates | 1,2,3,4 | **not started** | — | Implemented T1(app-code)→T2(container) on one branch, landed as one CI-green push (env collapse can't be green in halves — no shim). Frozen subdir constants `media/ apps/ os-images/` shared T1↔T2 |
| B3 | os-images serve-seam dangling-row self-heal (open-fail of `cached` row → re-enqueue tag + demote row, then 503); set `base_cache.state` before unlink in GC txn | 6 | **not started** | — | miss-tolerance; must precede B4 (sequencing rule). netboot_base.py:816 (gc), app.py:502-506 (serve seam) |
| B4 | os-images orphan sweep (FS-enum, writer-lock + in-flight-owned incl caching/mirroring + staging split, modeled on media_store.recover) + reserved floor + byte cap; alarm when keep-set > cap | 5 | **not started** | — | eviction — lands AFTER B3. Structured log fields on eviction/GC/orphan here |
| B5 | Remove hand-staging **routes** `register_app` (app.py:700-707) + `promote_app` (app.py:709-712); KEEP `AppPackages.register/.promote` (mirror+reconcile callers real); 410 assertion | 7 | **BLOCKED** | — | Precondition (decision 5, reqs 6/8): SQL must return 0 un-refetchable served `.deb`s. CI/DB only — cannot run in sandbox. Sequence last |
| docs | module-cache.md (new); architecture.md, module-pxe-service.md, runbook.md updates | — | **not started** | — | own bead per §3.6; closes the milestone |
| iac | Separate repo (mcurcio/iac): one `cache` PVC at /var/cache/photo-wall (worker RW, central RO) replacing media+app PVCs; set only cache-root; bump image tag; update tests/workloads/test_photo_wall.py | — | **not started** | — | Follows the app contract; gates on tracer CI-green |

OTEL metrics = explicitly a **separate post-Slice-1 bead** (no metrics seam in central/ today; only logging). Structured log fields ship inside each op-bead now.

## Precondition SQL (decision 5 — owner/CI must run against live DB)

Must return **0 rows** before B5 deletes the operator routes; non-empty → STOP, drain first.

```sql
WITH served_sha AS (
    SELECT current_sha256 AS sha FROM app_package_policy WHERE singleton
    UNION
    SELECT r.mirrored_sha256 AS sha
    FROM devices d
    JOIN app_releases r ON r.tag IN (d.attached_tag, d.known_good_tag, d.last_served_tag)
    WHERE r.mirrored_sha256 IS NOT NULL
)
SELECT p.sha256, p.version
FROM app_packages p
JOIN served_sha s ON s.sha = p.sha256
WHERE NOT EXISTS (SELECT 1 FROM app_releases r2 WHERE r2.mirrored_sha256 = p.sha256);
```

## Slices 2 & 3: NOT started until Slice 1 CI-green (see design §Delivery).
