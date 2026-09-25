# Bead 3: `catalog-follows-upstream`

**Design:** `docs/central-idempotent-jobs.md`: rule 2, §4 (the Release row, Frozen flag and
Automatic promotion rows), §6 (the whole section), §8 (migration 029), §11 (6, N2, R6).
**Base branch:** `claude/central-followups` with bead 1 landed.
**Follows:** bead 1. It runs in parallel with beads 2 and 4, each in its own worktree; the file
sets are disjoint.
**Packages:** `central/kernel` (`ports.py`), `central/origins`, `central/content_catalog`,
`central/infra` (`catalog_records.py`), `central/migrations`.

## Behaviour

1. **One observation per release.** The release row is inserted if absent; otherwise it is locked.
   The previous row and the frozen flag come from that locked row. The write is applied only if its
   upstream version is not older than the stored one. A refused observation touches nothing.
2. **The upstream version** is the manifest asset's `(updated_at, id)`. It is set only when the
   manifest body was read.
3. **The frozen flag** is derived from the locked previous row. It is never carried over.
4. **The stored ETag** is trusted for one hour. An operator refresh clears it.
5. **The automatic promotion** takes a transaction-scoped advisory lock, then reads the rows.
6. Per-release transactions, the ETag after the rows, and the tail after the ETag stay as they are.

## Frozen page

**Kernel** (`central/kernel/ports.py`)
- `UpstreamVersion`: a frozen, `order=True` dataclass `(changed_at: float, asset_id: int)`.
  - `changed_at` must be finite: `invalid_changed_at`.
  - `asset_id` must be an `int` above 0: `invalid_asset_id`.
- `PublishedRelease.upstream_version: UpstreamVersion | None`, with **no default**.

**Origin** (`central/origins/github.py`)
- `_asset_urls` also keeps the manifest asset's `id` and `updated_at`. `updated_at` is parsed with
  `datetime.fromisoformat` into epoch seconds.
- `upstream_version` is set only when `_fetch_manifest` returned a body. A missing manifest asset,
  a 404/410, or an invalid `id`/`updated_at` gives None.

**Migration `central/migrations/029_release_upstream_version.sql`**
- Add `app_releases.upstream_changed_at DOUBLE PRECISION` and `upstream_asset_id BIGINT`, with
  `CHECK ((upstream_changed_at IS NULL) = (upstream_asset_id IS NULL))`.
- Add `app_release_poll.etag_stored_at DOUBLE PRECISION`.
- `UPDATE app_release_poll SET etag = NULL`.
- Rollback: a code revert.

**Records** (`central/content_catalog/ports.py`, `central/infra/catalog_records.py`)
- `claim(self, tx, release: PublishedRelease, *, now: float) -> ReleaseRow | None` replaces
  `upsert`:
  1. `INSERT ... ON CONFLICT (tag) DO NOTHING`. If it inserted, return None: this first
     observation is applied.
  2. Otherwise, `SELECT ... FOR UPDATE` and return the locked previous row.

  Probed on PostgreSQL 16: a concurrent first insert waits, then sees the winner's row.
- `apply(self, tx, release, *, divergent: bool, now: float) -> bool`: the guarded
  `UPDATE ... WHERE tag = %s AND (upstream_changed_at IS NULL OR (%s IS NOT NULL AND
  (upstream_changed_at, upstream_asset_id) <= (%s, %s))) RETURNING tag`. True when applied.
  - `mirror_state`: `'divergent'` if `divergent`. Else, where it was `'divergent'`:
    `'discovered'` with a package, or `'undeployable'` without. Otherwise it is unchanged.
  - `mirror_error`: `'asset_changed'` or NULL, to match.
- `mark_divergent` and `upsert`: deleted.
- `load_etag(self, tx) -> StoredEtag | None`, where `StoredEtag(etag: str | None, stored_at: float)`.
- `store_etag(self, tx, etag: str | None, *, now: float) -> None`.
- `lock_auto_promotion(self, tx) -> None`: `SELECT pg_advisory_xact_lock(AUTO_PROMOTION_LOCK)`.
  `AUTO_PROMOTION_LOCK: Final` is a literal bigint, distinct from `central/db.py`'s migration lock.

**Sync** (`central/content_catalog/sync.py`)
1. `ETAG_MAX_AGE: Final = timedelta(hours=1)` (a placeholder). `handle` passes the stored ETag to
   the origin only if it was stored less than `ETAG_MAX_AGE` ago; otherwise it passes None.
2. `_record`:
   - `previous = claim(...)`;
   - if `previous` is not None, compute `frozen = _frozen_package(tx, release, previous)` from the
     locked row (no `get`), and if `not apply(..., divergent=frozen is not None)` return an empty
     set;
   - references and retires then follow as today.
3. `_frozen_package`: delete the `if previous.divergent: return old` branch.
4. `_auto_promote`: call `lock_auto_promotion(tx)` FIRST, then `promotion`, `all`,
   `bound_player_count` and the produced facts.
5. Update the module docstring.

**Catalog** (`central/content_catalog/catalog.py`, `refresh`): in one transaction, call
`store_etag(tx, None, now=...)`, then `publish(SyncReleases(), within=tx, retry_terminal=True)`;
then `publish_now(Prefetch())` as today.

**No new error codes.**

## Files

- **Code:** `central/kernel/ports.py`, `central/origins/github.py`,
  `central/content_catalog/ports.py`, `central/content_catalog/sync.py`,
  `central/content_catalog/catalog.py`, `central/infra/catalog_records.py`,
  `central/migrations/029_release_upstream_version.sql` (new).
- **Tests:** `tests/test_release_versions.py` (new), `tests/support/github_release.py`, plus the
  carried files below.

## Existing tests it carries

- Every `PublishedRelease(...)` names `upstream_version`:
  - `tests/content_db.py`, `tests/test_content_catalog_sync.py`, `tests/test_infra_catalog_records.py`;
  - `tests/test_kernel_ports.py`, `tests/test_netboot_e2e_wire.py`;
  - `tests/test_os_image_content_key.py`, which bead 1 created;
  - `tests/fakes/origin.py`, if it builds one.
- `tests/test_infra_catalog_records.py` and `tests/content_db.py:61`:
  - `mark_divergent` becomes `claim` + `apply(divergent=True)`;
  - the `load_etag`/`store_etag` callers take the new shapes.
- `tests/test_content_catalog_sync.py`:
  - the freeze test (`:214`) still holds, now derived;
  - every re-cut carries a newer version;
  - the transaction-count and unchanged-listing tests (`:157-189`) keep their counts.
- `tests/test_content_catalog_catalog.py`: `refresh` clears the ETag and publishes in one
  transaction.
- `tests/support/github_release.py`:
  - `release_entry` gives each asset an `id` and an `updated_at`;
  - `FakeRelease` carries a manifest version;
  - a helper re-cuts a release with a newer version.
- `tests/test_origins_github.py`: the version parsing.
- `tests/test_netboot_fresh_install_e2e.py`: must stay green.

## Acceptance criteria (`tests/test_release_versions.py`, PostgreSQL)

1. **V1 stale after fresh:**
   - v1 at version 20 (`.deb` B, S2) is applied;
   - v1 at version 10 (A, S1) is then refused;
   - expect no change to the row, the references or `mirror_state`, no changed keys, and no
     fetch published.
2. **V2 stale before fresh:** versions 10, then 20. The row matches 20, and A's reference is
   retired.
3. **V3 equal version:** re-applying 20 changes no key and publishes nothing, but a changed
   prerelease flag lands.
4. **V4 frozen is derived:**
   - A is produced;
   - B at version 20 freezes v1 at A;
   - A at version 30 unfreezes it;
   - a stale B at version 25 is refused.
5. **V5 promotion:** tail X holds the advisory lock. A newer release row commits elsewhere. Tail Y
   blocks on the lock. X commits, then Y promotes the newest tag.
6. **V6 migration:**
   - 029 applies on 028;
   - a half-set version pair is refused;
   - the ETag is cleared.
7. **V7 origin:** a manifest asset with `id` 7 and `updated_at` `2026-09-01T00:00:00Z`, whose body
   was read, yields `UpstreamVersion(1788220800.0, 7)`. The same listing with the manifest
   answering 404 yields None.
8. **V8 equal-version repair:**
   - a stale equal-version observation lands after a fresh one;
   - the fresh ETag is stored last;
   - a sync within the hour sends the ETag and changes nothing;
   - a sync after the hour sends none, and repairs the row;
   - `refresh` also forces a full listing.
9. **V9 no body, no wipe:** v1 is stored at version 20. A listing whose manifest answers 404 is
   refused, and v1's package and references stay.
10. **V10 concurrent first insert:** two transactions claim a new tag at once. The second sees the
    first's row as `previous`, and no reference is orphaned.

## Mutation probes

- M1: drop `apply`'s `WHERE` (V1).
- M2: restore the sticky branch (V4).
- M3: write references when `apply` is False (V1).
- M4: read the rows before `lock_auto_promotion` (V5).
- M5: trust the ETag forever (V8).
- M6: set the version on a 404 manifest (V9).
- M7: take `previous` from an unlocked read before the insert (V10).
- M8: leave the ETag in 029 (V6).

## Report back

Report the net line delta, the reuse you considered, and any errata appended where this page is
wrong. **Report where the spec is wrong.**
