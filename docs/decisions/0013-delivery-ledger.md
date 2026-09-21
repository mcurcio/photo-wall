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
| T (tracer) | ONE `PHOTO_WALL_CACHE_ROOT` + internal subdir derivation across 4 readers; release-sourcing + base-serving **always-on** (remove `app_root/base_root is None` gates + `503 release_sourcing_unconfigured`); Dockerfile (ARG PUID/PGID, GID pin, ENV cache-root, `install -d` 3 subdirs before `VOLUME`, `VOLUME` in central+media-worker leaves); entrypoint rewrite (install -d per subdir, drop per-domain env, keep numeric zero-uid guard); compose remount at cache root; in-slice test updates | 1,2,3,4 | **CI-GREEN** | T1 87c9a0b, T2 91b9c21 (PR #19, 6/6) | Landed as one CI-green push (env collapse can't be green in halves — no shim). Probes 1,2 validated by CI netboot-e2e. Central-entrypoint design ruling followed (worker single writer). E11+E12 (010 guard) |
| B3 | os-images serve-seam dangling-row self-heal (open-fail of `cached` row → re-enqueue tag + demote row, then 503); set `base_cache.state` before unlink in GC txn | 6 | **committed, CI-pending** | 176e73d | miss-tolerance; precedes B4. Reuses `enqueue_base_fetch_in`. Fast gate 1003/329; probe-6 DB test runs in CI. **E13**: GC reorder only shrinks the dangling window (unlink non-transactional) — serve-seam self-heal is the real guarantee; soften design:149 in docs bead |
| B4 | os-images orphan sweep (FS-enum, in-flight-owned incl caching + per-tag re-confirm + mtime grace, modeled on media_store.recover) + reserved floor + byte cap; alarm when keep-set > cap | 5 | **CI-GREEN** | f9f4a0a (PR #19 6/6) | eviction — lands AFTER B3. Deletion-safety review: F-1 (owned-set snapshot ≠ the frozen single-writer exclusion → per-tag re-confirm + 300s mtime grace, **E15**) + F-2 (missing-dir crash guard) fixed in cycle 1. **E14**: floor is by-construction (0012 keep-set evicts every non-keep tag), cap is the active alarm. Residual: `_BASE_FETCH_TIMEOUT_SECONDS` mirrors a constructor default (promote to importable = later cleanup) |
| B5 | Remove hand-staging **routes** `register_app` (app.py:700-707) + `promote_app` (app.py:709-712); KEEP `AppPackages.register/.promote` (mirror+reconcile callers real); 410 assertion | 7 | **BLOCKED (owner)** | — | Precondition (decision 5, reqs 6/8): SQL must return 0 un-refetchable served `.deb`s. CI/DB only — cannot run in sandbox. Sequence last; owner must run the SQL |
| docs | `module-central-cache.md` (new — `module-cache.md` was already taken by the Player disposable-cache doc; design/ledger were wrong to call it free) + architecture/pxe/appliance-release/player-package/media-worker; P2-2 drift (README, runbook); design-doc softening for E13/E14/E15 | — | **committed** | 29f4a72 | own bead per §3.6. B5's hand-staging-removal doc bits (module-appliance-release:88, module-player-package:50, runbook:124) + release.yml:288 fold in when B5 lands |
| iac | Separate repo (mcurcio/iac): one `cache` PVC at /var/cache/photo-wall (worker RW, central RO) replacing media+app PVCs; set only cache-root; bump image tag; update tests/workloads/test_photo_wall.py | — | **not started** | — | Follows the app contract; gates on tracer CI-green |

OTEL metrics = explicitly a **separate post-Slice-1 bead** (no metrics seam in central/ today; only logging). Structured log fields ship inside each op-bead now.

## Tracer (T) review outcome

Verifier: **PASS** (locally-verifiable scope) — 8/8 gates, fast suite 996/328/0, entrypoint 12/12, all mutation probes reversed→red→restored. Security review: **ship it** — no P0/P1; privilege drop, VOLUME ordering, empty-cache miss-tolerance, cache_layout, media_gateway gate, compose all attacked and cleared.

Fixed in the tracer:
- **P2-1** — zero-uid guard admitted `010`; tightened the `case` to reject leading-zero multi-digit (canonical positive decimal now construction-time) — matches design:191. Errata **E12** (closes E11's flagged gap).
- **probe-3 gap** (verifier) — added a positive regression test: legacy `PHOTO_WALL_{MEDIA,APP,BASE}_ROOT` set alongside `CACHE_ROOT` → resolved paths unaffected.

Deferred to the **docs bead** (never fail a code bead on docs, §3.6):
- `README.md:110` (`PHOTO_WALL_BASE_ROOT` "required/off" — now false), `docs/runbook.md:172` (`503 release_sourcing_unconfigured` — branch deleted).
- `.github/workflows/release.yml:288` — release-notes still tell operators to copy `.deb` to `PHOTO_WALL_APP_ROOT`; tied to hand-staging → fold into **B5**.

CI watch-items (not code defects — verify when the PR runs):
- **W1 (primary)** — worker boot now polls GitHub unconditionally (always-on flip). Previously-sealed e2e/demo jobs may make live GitHub calls / hit the 60/hr unauth rate limit. Failures are swallowed (non-fatal), but watch netboot-e2e/software-e2e/demo for boot-time GitHub flakiness.
- **W2** — `release.yml` retained-native media base: a stale cached base predating 0013 lacks the `PHOTO_WALL_PUID/PGID` ENV → `install -d -o ''` build fail. PR gate is safe (service-base rebuilds fresh from the current Dockerfile, ENV confirmed at Dockerfile:13-16); **release-time** watch only.
- **W3 (minor)** — central-only topology (unsupported per design:194-195): promote now enqueues with no worker consumer → silent hang instead of the old 503. By-design; note for the operator runbook.
- **W4 (minor)** — vestigial always-true `base_root is not None` conditionals (app.py:778,793; netboot_base.py:283 `base_root_configured` now constant; dead None-branch in `assert_base_root_writable`). Cleanup candidate, not a bug → `residual:` if worth it.

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
