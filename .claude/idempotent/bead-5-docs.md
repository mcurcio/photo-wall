# Bead 5: `idempotent-jobs-docs`

**Design:** `docs/central-idempotent-jobs.md` (the whole document; this bead links to it).
**Base branch:** `claude/central-followups` with beads 1 to 4 landed.
**Follows:** beads 1, 2, 3 and 4. It is docs only, and it never fails or reverts a code bead.

## Behaviour

`docs/central-system-architecture.md` says exactly what beads 1 to 4 built. The open errata
corrections are applied, and the runbook documents the release API base.

## Changes

**`docs/central-system-architecture.md`**
- §0 row 3 ("A worker dies holding a job"): rescue takes a queueing lock only. Rescue still
  re-publishes; it never retries in place.
- §4 intro: results are ordered by start. The `SyncReleases` row: one transaction per sync, with
  "frozen" recomputed. The `RescueStalledJobs` row: no running lock.
- §6(c): correct "merges into a pending or running copy". With a copy running, a new pending row is
  inserted and runs afterwards (errata 2026-09-23).
- §6(d): a paused worker may finish late, and its result is refused if a later-started run stored
  one.
- §9 decision 3: ruling 3a applies to every job type (a recorded `terminal` may run once more if its
  worker dies after recording).
- §10.1: `Delivery.concurrent`, and what `JobKeys.lock` means.
- §10.2: the start-number ordering in `job_outcomes`, and `superseded`.
- §10.3: `commit_failed` and the 2s worker idle limit.
- §10.4: `ReleaseRecords.set_divergent`.
- Add a link to `central-idempotent-jobs.md`.
- The open errata corrections (`grep -a` `.claude/errata.md`):
  - #24 (errata lines 899-927): §6(b)'s "No job is published" holds only when the first candidate
    is opened; the §10.2 `read` sketch; §3's Assets row; §4's Published-by column ("HTTP
    substitute serve").
  - The 2026-09-24 entry: the substitute publish is synchronous, inside the open's transaction, and
    not in the background.
  - The two-pod entry: §3 "otherwise Unknown → 404" holds only while the catalog is empty.
  - #25: the §5 "Desired set" row says "served within the last 30 days". §3's Catalog row and §5's
    Catalog-entries row say that a promotion records who set it, and that the sync moves only its
    own.

**`docs/runbook.md:152` (the release configuration table):** add a row.

| Variable | Where | Default | Meaning |
|---|---|---|---|
| `PHOTO_WALL_RELEASE_API_BASE` | worker | GitHub's API | The base URL of the releases API. An unset or empty value means the default. Tests point it at a fake origin |

## Acceptance criteria

- `python3 scripts/check_docs.py` passes.
- Every item above is present, and the text matches the landed code (verifier: spot-check the
  §10.1–§10.4 signatures against the code).
