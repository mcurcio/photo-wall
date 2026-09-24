# Bead 6: `idempotent-jobs-docs`

**Design:** `docs/central-idempotent-jobs.md` (the whole document; this bead links to it).
**Base branch:** `claude/central-followups` with beads 1 to 5 landed.
**Follows:** beads 1 to 5. It is docs only, and it never fails or reverts a code bead.

## Behaviour

The architecture, the runbook and the cache module say exactly what beads 1 to 5 built. The open
errata corrections are applied.

## Changes

**`docs/central-system-architecture.md`**
- Link to `central-idempotent-jobs.md` from §4 and §5.
- §0, row "A worker dies holding a job": rescue has no one-runner lock; it still re-publishes and
  never retries in place.
- §4:
  - the intro: jobs may run twice, late or out of order, and each data type converges by its own
    rule;
  - `FetchOsImage(tarball_sha256)`: its subject is the base tarball's sha256;
  - `SyncReleases`: guarded by upstream version, and "withdraw" is not in the MVP (sync.py says
    so);
  - `RescueStalledJobs`: no one-runner lock.
- §5:
  - Asset key `(os-image, tarball sha256)`; the row and its facts stay after the last reference
    (the cache sweep removes them);
  - Catalog entries store the upstream version;
  - the Job outcome row: `ok` comes with its data, a failure is kept only while the data is
    absent, and a terminal stands until a request withdraws it.
- §6(b) (errata #24, lines 899-927): "No job is published" holds only when the first candidate is
  opened. The substitute publish is synchronous, inside the open's transaction (errata
  2026-09-24).
- §6(c): with a copy running, a publish inserts a pending row that runs afterwards (errata
  2026-09-23). `FetchOsImage(tag)` becomes `FetchOsImage(tarball_sha256)` throughout.
- §6(d): a paused worker may finish late; its late failure is dropped when the data is present.
- §9, decision 3: add 3a and the withdraw (PB3a).
- §10.1: `Delivery.concurrent`, what `JobKeys.lock` means, and `FetchOsImage`'s field.
- §10.2: PB3a; `record` returning None; `withdraw_terminal`; `Candidates.labels`; the reader tries
  the data first. Fix the `read` sketch (`# on disk: publish nothing`, errata #24).
- §10.3: `settled`; `DataPresence` and `LockedAssetPresence`.
- §10.2 and §10.4: `forget_produced` is gone (§10.2 at about line 384). `retire` keeps the row.
  `ReleaseRecords.upsert(..., divergent) -> ReleaseWrite`, `lock_promotion`; `mark_divergent` is
  gone.
- The other open errata items:
  - §3 "otherwise Unknown -> 404" holds only while the catalog is empty (the two-pod entry);
  - §3's Assets row, and §4's Published-by column: "HTTP substitute serve" (errata #24);
  - #25: the §5 "Desired set" row says "served within the last 30 days";
  - #25: §3's Catalog row and §5's Catalog-entries row say that a promotion records who set it,
    and that the sync moves only its own.

**`docs/runbook.md`**
- The release configuration table (about :152): add a row.

| Variable | Where | Default | Meaning |
|---|---|---|---|
| `PHOTO_WALL_RELEASE_API_BASE` | worker | GitHub's API | The base URL of the releases API. An unset or empty value means the default. Tests point it at a fake origin |

- :204 and :228: OS images are `base-<tarball sha256>.squashfs`. After the upgrade to migration 028,
  each desired OS image is downloaded once, and legacy `base-<tag>.squashfs` files may be deleted
  by hand.

**`docs/module-central-cache.md:102`**: the `base-<tag>.squashfs` naming becomes the tarball sha.

## Acceptance criteria

- `python3 scripts/check_docs.py` passes.
- Every item above is present, and the text matches the landed code. The verifier spot-checks the
  §10 signatures against the code.
