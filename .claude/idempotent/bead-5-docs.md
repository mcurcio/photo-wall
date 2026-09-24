# Bead 5: `idempotent-jobs-docs`

**Design:** `docs/central-idempotent-jobs.md` (the whole document; this bead links to it).
**Base branch:** `claude/central-followups` with beads 1 to 4 landed.
**Follows:** beads 1 to 4. It is docs only, and it never fails or reverts a code bead.

## Behaviour

The architecture, runbook, README and cache module say exactly what beads 1 to 4 built. The open
errata corrections are applied. The runbook carries the owner's upgrade procedure.

## Changes

**`docs/central-system-architecture.md`**
- Link to `central-idempotent-jobs.md` from §4 and §5.
- §0, row "A worker dies holding a job": only asset fetches take the one-runner lock. Rescue
  re-publishes and never retries in place.
- §4:
  - the intro: jobs may run twice, late or out of order, and each data type converges by its own
    rule;
  - `FetchOsImage(tarball_sha256)`;
  - `SyncReleases` is guarded by upstream version, and withdrawal is not in the MVP;
  - the Published-by column: "HTTP substitute serve" (errata #24).
- §5:
  - the Asset key is `(os-image, tarball sha256)`, and a row and its facts outlive their last
    reference;
  - Catalog entries store the upstream version;
  - the Job outcome row is last-write-wins, and readers decide from the data;
  - the "Desired set" row says "served within the last 30 days" (#25);
  - the Catalog-entries row says a promotion records who set it, and that the sync moves only its
    own (#25).
- §6(b): "No job is published" holds only when the first candidate is opened. The substitute
  publish is synchronous, inside the open's transaction (errata #24 and 2026-09-24).
- §6(c): with a copy running, a publish inserts a pending row that runs afterwards (errata
  2026-09-23). `FetchOsImage(tag)` becomes `FetchOsImage(tarball_sha256)` throughout §6.
- §6(d): a paused worker may finish late; readers still serve the data that is present.
- §3: "otherwise Unknown -> 404" holds only while the catalog is empty (the two-pod entry). Its
  Assets and Catalog rows are updated as for §4 and §5.
- §9, decision 3: add 3a, and the 30-day purge of notes.
- §10.1: which jobs take the one-runner lock; `FetchOsImage`'s field.
- §10.2: fix the `read` sketch (`# on disk: publish nothing`, errata #24); the reader tries the
  data after a wait; `forget_produced` is gone (about line 384).
- §10.4:
  - `retire` keeps the row;
  - `ReleaseRecords.claim` and `apply`, `lock_auto_promotion`, `StoredEtag`;
  - `mark_divergent` and `upsert` are gone;
  - `WriteFn` takes a locator.

**`docs/runbook.md`**
- The release configuration table (about :152): add the row below.

| Variable | Where | Default | Meaning |
|---|---|---|---|
| `PHOTO_WALL_RELEASE_API_BASE` | worker | GitHub's API | The base URL of the releases API. An unset or empty value means the default. Tests point it at a fake origin |

- :204: OS images are `os-images/base-<tarball sha256>.squashfs`.
- :228: replace the "Garbage collection" paragraph. No collector exists yet; files and
  unreferenced asset rows stay until `MaintainCache`.
- **New "Upgrading to content-keyed OS images (migration 028)" procedure**, stating the owner's
  decision and its cost plainly:
  1. **Preflight.** Confirm the worker reaches the release origin. For every desired tag (pins,
     known-goods, recently served, frontier or bootstrap), check that its tarball URL answers.
  2. Upgrade Central and workers together.
  3. Delete the old `os-images/base-<tag>.squashfs` files: they are orphaned.
  4. Each desired OS image downloads once. A Pi that reboots before its image lands fails and
     reboots again. If GitHub is unreachable or a tarball was deleted upstream, Pis loop on
     503 and reboot until a download succeeds. A device pinned to a deleted release stays stuck
     until it is re-pinned.

**`README.md:110`**: `os-images/base-<tag>.squashfs` becomes the tarball sha. Drop any claim of
automatic eviction.

**`docs/module-central-cache.md:102`**: the `base-<tag>.squashfs` naming becomes the tarball sha.

## Acceptance criteria

- `python3 scripts/check_docs.py` passes.
- Every item above is present, and the text matches the landed code. The verifier spot-checks the
  §10 signatures against the code.
