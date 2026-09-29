# Unsaved Source match preview — 2026-09-29

**Evidence class:** synthetic Immich adapter checks, disposable PostgreSQL and
Procrastinate worker integration, and local Chromium against the operator API.
This change extends draft [PR #38](https://github.com/mcurcio/photo-wall/pull/38).
The last read-only Kubernetes inspection still showed Central at `v0.12.0`;
this branch has not been deployed to the live installation.

On the Photo sources **What to include** step, an operator can now request a
match count for the current media type, favourites, date window, and chosen
worker connection before saving. A completed observation shows total, image,
and video counts; zero is shown only for a completed empty query. Permission,
version, connection, transport, and bounded-search failures have distinct
nonempty outcomes. The count describes query matches, not prepared media or
Frame compatibility. A Source is still saved explicitly and then refreshed by
the existing worker lifecycle.

Central validates and transactionally enqueues an opaque, short-lived preview
request. The media worker reads its private connection configuration and uses
the existing bounded Immich search and eligibility checks, without EXIF,
original downloads, media preparation, or Source/catalog publication. It
reports an exact count only after a complete bounded scan. The worker and
Central never send the Immich URL, API key, owner ID, or raw upstream rows to
the browser. Pending requests expire after ten minutes; completed and failed
receipts are retained for up to another hour and all records are capped. The
UI can resume polling an accepted request after leaving the Include step.
Changes to draft criteria hide prior results, and preview requests do not mark
the Source draft dirty.

## Verification

- Portable suite: **2,551 passed, 955 skipped, three warnings**. The first
  run found one task-registration assertion that omitted the new task; the
  assertion was corrected and the full portable suite then passed.
- Local Compose PostgreSQL suite: **3,187 passed, 315 skipped, one documented
  expected failure, four warnings**. A later focused test additionally
  exercised POST persistence through the real Procrastinate task runner,
  worker evaluation, and terminal GET state: **one passed**.
- Full local Chromium operator walkthrough: **303 passed, three warnings**.
  Preview browser cases use synthetic endpoint replies and cover exact filters,
  empty/failure/unknown outcomes, connection choice, invalid dates, stale
  answers, and resuming the same accepted request after navigation.
- Ruff, production console build, documentation links (102 Markdown files), and `git diff --check`
  passed.

The synthetic adapter and browser checks do not establish behavior against the
user's real Immich instance. This run did not exercise live Kubernetes rollout,
actual HDMI pixels, or physical Player timing.
