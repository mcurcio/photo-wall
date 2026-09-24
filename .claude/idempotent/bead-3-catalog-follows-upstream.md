# Bead 3: `catalog-follows-upstream`

**Design:** `docs/central-idempotent-jobs.md`, rule 2, §4 (Release row, ETag, Frozen flag and
Automatic promotion rows), §5 second walkthrough, §8 (migration 029), §11 (6a, 6b, R1, N2, N4).
**Base branch:** `claude/central-followups` with bead 1 landed.
**Follows:** bead 1 (it rewrites `_record`, which bead 1 changes first). It runs in parallel with
beads 2 and 4, each in its own worktree; the files are disjoint.
**Packages:** `central/kernel` (`ports.py`), `central/origins`, `central/content_catalog`,
`central/infra` (`catalog_records.py`), `central/migrations`.

## Behaviour

A release observation is applied only if its upstream version is not older than the stored one.
The upstream version is the `manifest.json` asset's `(updated_at, id)`. A refused observation
touches nothing: no row, no reference, no retire, no freeze, and no changed key. The frozen flag
is computed inside that same guarded write from the stored `.deb`, upstream's `.deb` and the
produced facts. It is never carried from the previous flag. The automatic promotion takes the
policy row's lock before it reads the release rows. Per-release transactions, the ETag after the
rows, and the tail after the ETag are unchanged.

## Frozen page

**Kernel** (`central/kernel/ports.py`)
- `UpstreamVersion`: a frozen, `order=True` dataclass `(changed_at: float, asset_id: int)`.
  `changed_at` must be finite (`invalid_changed_at`), and `asset_id` a positive `int`
  (`invalid_asset_id`).
- `PublishedRelease.upstream_version: UpstreamVersion | None`, with **no default**. None when the
  release has no manifest asset, or its `id` or `updated_at` is missing or invalid.

**Origin** (`central/origins/github.py`): `_asset_urls` also returns the manifest asset's `id` and
`updated_at`. `updated_at` is parsed with `datetime.fromisoformat` into epoch seconds. `_resolve`
fills `upstream_version`.

**Migration `central/migrations/029_release_upstream_version.sql`**
```
ALTER TABLE app_releases ADD COLUMN upstream_changed_at DOUBLE PRECISION,
                         ADD COLUMN upstream_asset_id BIGINT,
    ADD CHECK ((upstream_changed_at IS NULL) = (upstream_asset_id IS NULL));
UPDATE app_release_poll SET etag = NULL;
```
Rollback is a code revert: old code never names the columns.

**Records** (`central/content_catalog/ports.py`, `central/infra/catalog_records.py`)
- `ReleaseWrite`: a frozen dataclass `(applied: bool, previous: ReleaseRow | None)`.
- `ReleaseRecords.upsert(self, tx, release, *, divergent: bool, now: float) -> ReleaseWrite`:
  - keep the `SELECT ... FOR UPDATE` of the previous row;
  - the `ON CONFLICT (tag) DO UPDATE` gains exactly this guard (probed on PostgreSQL 16, including
    two concurrent first inserts):
    `WHERE app_releases.upstream_changed_at IS NULL OR (EXCLUDED.upstream_changed_at IS NOT NULL
    AND (app_releases.upstream_changed_at, app_releases.upstream_asset_id) <=
    (EXCLUDED.upstream_changed_at, EXCLUDED.upstream_asset_id)) RETURNING tag`;
  - `applied` is whether a row came back;
  - `mirror_state` on update: `'divergent'` if `divergent`; else, where the stored state is
    `'divergent'`, `'discovered'` with a package or `'undeployable'` without; otherwise unchanged.
    `mirror_error` is `'asset_changed'` or NULL to match. On insert, as today.
- `ReleaseRecords.mark_divergent`: deleted.
- `ReleaseRecords.lock_promotion(self, tx) -> Promotion | None`: `SELECT promoted_tag,
  promoted_by FROM app_release_policy WHERE singleton FOR UPDATE`.

**Sync** (`central/content_catalog/sync.py`)
1. `_frozen_package`: delete the `if previous.divergent: return old` branch. A tag is frozen when
   its stored `.deb` exists, upstream's differs or is absent, and the stored sha's asset has
   produced facts.
2. `_record`: compute `frozen`, then call `upsert(..., divergent=frozen is not None)`. If
   `not applied`, return an empty set and write nothing else.
3. `_auto_promote`: call `lock_promotion(tx)` FIRST and use its result as `promotion`; only then
   read `all`, `bound_player_count` and the produced facts.
4. Update the module docstring: observations are guarded by upstream version, and "frozen" is
   derived.

**No new error codes.**

## Files

- **Code:** `central/kernel/ports.py`, `central/origins/github.py`,
  `central/content_catalog/ports.py`, `central/content_catalog/sync.py`,
  `central/infra/catalog_records.py`, `central/migrations/029_release_upstream_version.sql` (new).
- **Tests:** `tests/test_release_versions.py` (new), `tests/support/github_release.py`, plus the
  carried files below.

## Existing tests it carries

- Every `PublishedRelease(...)` names `upstream_version`: `tests/content_db.py`,
  `tests/test_content_catalog_sync.py`, `tests/test_infra_catalog_records.py`,
  `tests/test_kernel_ports.py`, `tests/test_netboot_e2e_wire.py`, and `tests/fakes/origin.py` if it
  builds one.
- `tests/test_infra_catalog_records.py` (`mark_divergent` at about :100 and :230-235): use
  `upsert(divergent=True)`. `tests/content_db.py:61`: the seed does the same.
- `tests/test_content_catalog_sync.py:214` (the freeze) still holds, now derived. Every test that
  re-cuts a release gives the re-cut a newer version.
- `tests/support/github_release.py`: `release_entry` gives each asset an `id` and an `updated_at`.
  `FakeRelease` carries a manifest version, and a helper re-cuts a release with a newer one.
- `tests/test_origins_github.py`: parse the version, and give None for a missing or invalid field.
- `tests/test_netboot_fresh_install_e2e.py`: must stay green (its listings may lack versions).

## Acceptance criteria (`tests/test_release_versions.py`, PostgreSQL)

1. **V1 stale after fresh:** v1 at version 20 (`.deb` B, S2) is applied. v1 at version 10 (A, S1)
   is then refused. The row, its references and `mirror_state` are unchanged, no key changes, and
   no fetch is published.
2. **V2 stale before fresh:** 10, then 20. The row matches 20, and A's reference is retired.
3. **V3 equal version:** re-applying 20 changes no key and publishes no fetch.
4. **V4 frozen is derived:** A is produced. B at version 20 freezes v1 at A. A at version 30
   unfreezes it. A stale B at version 25 is refused, and v1 stays unfrozen.
5. **V5 promotion under the lock:** tail X holds `lock_promotion`. A newer release row commits in
   another transaction. Tail Y starts and blocks on the lock. X commits, then Y promotes the newest
   tag. Y must read the rows only after taking the lock.
6. **V6 migration:** 029 applies on 028. It adds both columns and the CHECK (a half-set pair is
   refused), and clears the ETag.
7. **V7 origin:** a listing whose manifest asset has `id` 7 and `updated_at`
   `2026-09-01T00:00:00Z` yields `UpstreamVersion(1788220800.0, 7)`.

## Mutation probes

- M1: drop the upsert's `WHERE` (V1 red).
- M2: restore the sticky branch (V4 red: v1 stays frozen at version 30).
- M3: write references when `applied` is False (V1 red).
- M4: read the release rows before `lock_promotion` (V5 red).
- M5: leave the ETag in 029 (V6 red).

## Report back

Report the net line delta, the reuse you considered, and any errata appended where this page is
wrong. **Report where the spec is wrong.**
