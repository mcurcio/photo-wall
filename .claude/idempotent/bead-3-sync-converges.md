# Bead 3: `sync-converges`

**Design:** `docs/central-idempotent-jobs.md`, rule 3, §5 note 2, §6 (the tail) and §11 (defect 6, R1).
**Base branch:** `claude/central-followups`.
**Follows:** none. It builds in parallel with beads 1, 2 and 4 in its own worktree: its files are
disjoint, though it shares the `central/infra` package with bead 1.
**Packages:** `central/content_catalog` and `central/infra` (one file).
**Out of scope:** do NOT build the prepare/apply handler split. A stale listing landing last is
residual R1, and it heals at the next sync.

## Behaviour

`SyncReleases` writes the whole listing, the ETag and the tail in ONE transaction. Two syncs
therefore never interleave, and a failure anywhere leaves nothing, so the next sync redoes all of
it. "Divergent" (frozen) is recomputed from facts on every sync; the sticky freeze is gone.

## Frozen page

**Signatures**
- `ReleaseRecords.set_divergent(self, tx: Transaction, tag: str, divergent: bool) -> None`
  (`central/content_catalog/ports.py`) replaces `mark_divergent`. The implementation lives in
  `central/infra/catalog_records.py`:
  - `True`: `mirror_state='divergent'` and `mirror_error='asset_changed'`.
  - `False`: only where `mirror_state='divergent'`, restore `mirror_state` to `'discovered'` if the
    tag has an `asset_sha256`, else to `'undeployable'`, and set `mirror_error=NULL`.

**Handler behaviour** (`central/content_catalog/sync.py`)
1. `handle` loads the ETag and lists releases (as today, outside any write), then makes ONE
   `in_transaction(transactions, …)` call. That call records every release (unless the listing is
   unchanged), stores the ETag (unless unchanged), and runs `_tail` with the changed set.
2. `_frozen_package` deletes the `if previous.divergent: return old` branch. A tag is frozen when
   all of these hold:
   - its stored `.deb` exists;
   - upstream's `.deb` differs or is absent;
   - the stored sha's asset has produced facts.
3. `_record` calls `set_divergent(tx, tag, frozen is not None)` for every recorded release.
4. **The tail joins the transaction.** Today a crash between the ETag commit and the tail loses the
   changed set for good: the next sync sees "unchanged", and `Prefetch` never retries a terminal.
   `publish(within=tx)` is already savepoint-safe (PB5).
5. Update the module docstring: one transaction per sync, and "frozen" is recomputed.

**No migration, no new error codes.**

## Files

- **Code:** `central/content_catalog/sync.py`, `central/content_catalog/ports.py`,
  `central/infra/catalog_records.py`.
- **Tests:** `tests/test_content_catalog_sync.py`, `tests/test_infra_catalog_records.py`,
  `tests/content_db.py`.

## Existing tests it carries

- `tests/test_content_catalog_sync.py`:
  - `:157-180`: transaction count 5 → 2 (the ETag load, plus the one write);
  - `:182-189`: the unchanged listing (still 2);
  - `:214-233`: the freeze still holds;
  - `:252-279`: the re-cut OS image's `retry_terminal` publish, inside the one transaction;
  - `:300-318`: an origin failure writes nothing, and a failing release keeps the ETag;
  - `:450-475`: an operator promotion committed during the sync still wins.
- `tests/test_infra_catalog_records.py:100` and `:230-235`: `mark_divergent` becomes
  `set_divergent(True)`. Add a test that `False` clears it.
- `tests/content_db.py:61`: the seed uses `set_divergent(tx, tag, True)`.

## Acceptance criteria (PostgreSQL)

1. **T4:**
   - Stale then fresh: a stale listing (the tag's `.deb` shows the old sha, and the new sha has been
     produced) lands and freezes the tag. A fresh listing then lands, and the tag is not frozen and
     its rows match the fresh listing.
   - Fresh, stale, then a third sync with the fake origin honouring `If-None-Match`: the third sync
     sees the stored stale ETag differ and heals the catalog (R1's heal).
2. **T4-atomic:** a failure while recording the second release, or inside the tail (the publisher
   raises), leaves no release rows, the previous ETag, and no publishes.
3. **Unfreeze:** upstream returns to the produced sha, and the tag is no longer divergent.

## Mutation probes

- M1: restore the sticky branch (T4 red).
- M2: restore per-release transactions (T4-atomic red, recording case).
- M3: run the tail in its own transaction (T4-atomic red, tail case).

## Report back

Report the net line delta, the reuse you considered, and any errata appended where this page is
wrong.
