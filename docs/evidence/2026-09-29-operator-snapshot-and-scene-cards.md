# 2026-09-29 operator snapshot and recursive Scene cards

## Change

The operator console's shared Plane A now comes from authenticated
`GET /v1/operator/snapshot`. Central reads inventory, Runtime and media in one
PostgreSQL `REPEATABLE READ READ ONLY` transaction. The response's `read_at` is
the common application-clock time for the snapshot and Runtime projection.
Accepted Player reports are read in the same MVCC view; the separate
`player_reports_read_at` records the safe report-age reference, clamped to at
least every accepted report timestamp. Inventory liveness timing also accounts
for enrollment timestamps. These report timestamps support honest age labels;
they do not confirm visible playback or lit pixels.

The existing authenticated `/v1/operator/inventory`, `/runtime`, and `/media`
GET routes retain their response shapes for compatibility. The console now uses
the aggregate endpoint because separate requests cannot guarantee one shared
database view.

Scene cards now summarize stored Scene content recursively across root and
inline-child body/outro contributions. Frame targets, live Source references,
and authored asset references are de-duplicated and sorted. The card can show
the complete Frame set, live Sources, and distinct authored-item count. This is
a content summary, not a readiness or visible-output claim.

## Focused qualification

- Backend tests: **32 passed**.
- Console/frontend checks: **39 passed**.
- Broader Chromium checks: **20 passed**, plus **2 aggregate-snapshot cases**.
- Scene-card focused checks: **24 passed**, plus **2 Chromium cases**.
- Production Vite build: passed; **120 modules** transformed.
- Final portable suite: `.venv/bin/python -m pytest -q` — **2,561 passed,
  984 skipped, 3 warnings** (163.27 s).
- Ruff passed. `python3 scripts/check_docs.py` checked **107 Markdown
  documents**.
- Final integrated local PostgreSQL suite: `.venv/bin/python scripts/test_local.py -q`
  — **3,214 passed, 330 skipped, 1 xfailed, 4 warnings** (507.20 s). The
  expected xfail is the documented legacy media-writer case; skips are
  environment-gated checks.

These are local software and PostgreSQL results only. No live deployment, live
Immich, Player readiness, physical Pi, panel scanout, or visible playback
qualification is claimed.
