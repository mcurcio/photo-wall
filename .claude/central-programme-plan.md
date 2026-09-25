# Central content-serving programme — resume document

Branch `claude/central-followups`, PR #27. Updated 2026-09-24 (#26 landed, beads 1–5).

## Done (PR #27)

- **#23:** a promotion records who made it (auto or operator). A sync never moves an operator
  promotion; this is enforced in the write. The 027 backfill uses main's rule.
- **#24:** serving a substitute also publishes the wanted asset's fetch, in the transaction that
  opens it.
- **#25:** a served tag stays desired for 30 days.
- **Two-pod run:** passed, and now runs in CI as `tests/test_two_pods.py`.
- **Netboot Compose tracer:** runs only in CI (`netboot-e2e.yml`). It passes on #27.
- **#26 (landed, beads 1–5):** jobs may run in any order; each data type converges by its own
  rule (`docs/central-idempotent-jobs.md`, status implemented). Bead 1 `os-image-content-key`
  (migration 028), bead 2 `reader-data-first`, bead 3 `catalog-follows-upstream` (migration 029),
  bead 4 `rescue-lock-free`, bead 5 `idempotent-jobs-docs` (architecture, runbook upgrade
  preflight and 028 rollback). Divergences from the pages are in `.claude/errata.md`.
- **Docs:** they match the landed code.

## Residuals

- **Test gap (bead 2's verifier):** no test covers a `Pending` outcome with the data present at
  the post-wait open (`central/assets/reader.py` `_produce_and_open`). Add one.

## Remaining, sized (about 31–36 beads)

Constraints:
- Migrations are numbered in order, and #26 takes 028 and 029.
- New job types all edit `job_types.py` and `content_wiring.py`, so they run one at a time.
- Most slices touch the files the #26 beads change, so they wait for those beads.

| Item | Slices (beads) | Depends on | Triggers |
| --- | --- | --- | --- |
| Fleet health | H1 re-add `failing_since` (COALESCE while not ok) (1). H2 authed `/v1/operator/health`: heartbeats, origin unreachable, wanted asset failing > 2h (1–2). H3 console region (1) | #26 bead 1 (no longer touches outcomes) | authz; health cannot import psycopg (new infra reader behind a port) |
| MaintainCache | C1 LRU under a byte budget, never desired; an asset-enumeration port (1–2). C3 orphan + temp sweep incl. `apps/`, AND unreferenced asset rows with their files (after #26 each re-cut leaves ~1 GiB and a bare row) (1). C2 over-budget warning, computed when read (1) | #26 bead 1; C2 after H2 | none strong |
| Cleanup | X1a origin listing completeness (1). X1b release withdrawal (1; owner decision: keep references while a device names the tag?). X2a `frozen` column replaces `mirror_state` (1). X2b drop legacy tables and columns (1). X3 worker root → `central/worker.py` (1). X4 netboot operator view, API + console (2) | X1b after bead 3; X3 after M6; X4 after C1, H2 | new top-layer module; the unauth `.deb` route changes |
| Media | M0a prepare/apply split (2). M0b facts-in-run (1). M0c content-keyed media variants (1; OS images already done by #26 bead 1). M0d stop reason (1). M1 vocabulary + two-field identity + migration (1). M2 PrepareMedia tracer (2). M3 SyncMediaSources fan-out (2). M4 coordination + desired set (1–2). M5 `/v1/media/{original}/{recipe}`, player + Central together (2). M6 retire the legacy loop, lease queue, flock (1). M7 drop the media tables (1) | M0a after #26 beads 1 and 3. M0a/M0b came from the rejected start-order design: re-size them at media design time against the per-data rules | kernel one-field rule (`kernel/jobs.py:189-192`); new package; player authz move; >4 beads |

## Recommended order

1. ~~#26 beads 1–5.~~ Done.
2. H1, then H2.
3. C1, then C3. REVERSED by #26: content-keyed OS images land first, so orphaned
   `base-*.squashfs` files and bare asset rows accumulate (~1 GiB per re-cut) until C3.
4. X1a, X1b, X2a, X2b. This can run in parallel with steps 2–3.
5. M0a–M0d, then M1. (R1 and R2 for OS images are closed by #26.)
6. M2 and M3 in parallel, then M4, then M5.
7. M6, M7, then X3.
8. C2 and X4.

## Still-stale docs (fold into cleanup)

- `docs/module-appliance-release.md`: the 0012 section and the upper half
  (`ReleaseAuthority`, `/v1/player/boot-health`)
- `docs/module-central-cache.md`: the status line and "os-images miss-tolerance and self-heal"
  (`base_cache`, `enqueue_base_fetch_in`: both gone)
- `docs/runbook.md` 0012 section: "latest-verified", `base_cache` rows and eviction reasons in
  the `/v1/operator/netboot` paragraph
- `docs/module-player-package.md:56`: "only on an operator promote" (the desired set fetches too)
- `central/infra/runtime.py:18`: "its lock blocks the key forever" (only an asset fetch has one)
