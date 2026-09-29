# 2026-09-29 current Player readiness diagnostics

## Change

The operator snapshot now carries assignment-level diagnostics projected from
the latest accepted Player readiness report and its exact offered plan. Central
limits the projection to the current non-retired Player epoch, highest offer
revision and its expiry, the current Player configuration and Frame binding,
non-ended layers, and the accepted-report silence threshold. It reuses
`player_feedback` and `plan_offers`; no diagnostic table or second mutable state
was added.

The Commissioning view maps known failure codes to recovery instructions and
uses generic guidance for unknown codes. Failure details age from Central's
accepted-report timestamp. Player-reported observation time is kept distinct,
and neither readiness nor enrollment's connected-Output observation claims
that pixels are visible.

## Focused qualification

- Projector PostgreSQL tests: **7 passed**. Coverage includes multi-Output
assignment resolution, superseded and expired offers, stale binding, stale
epoch, ended layers, and silent feedback.
- Aggregate snapshot plus projector PostgreSQL tests: **12 passed**.
- Operator-route focused PostgreSQL suite: **23 passed**.
- One escalated Chromium browser case passed. It used synthetic diagnostic
  injection to exercise the Commissioning UI; backend projection and UI
  behavior were tested separately, so this does not establish live
  backend-to-visible-UI integration.
- Final portable suite: `.venv/bin/python -m pytest -q` — **2,561 passed,
  984 skipped, 3 warnings** (163.27 s).
- Ruff passed. `python3 scripts/check_docs.py` checked **107 Markdown
  documents**.
- Final integrated local PostgreSQL suite: `.venv/bin/python scripts/test_local.py -q`
  — **3,214 passed, 330 skipped, 1 xfailed, 4 warnings** (507.20 s). The
  expected xfail is the documented legacy media-writer case; skips are
  environment-gated checks.
- One existing Starlette `BlockingPortal` deprecation warning appeared in local
  runs.

These are focused local database/software results. No live Player, display
readback, visible playback, physical panel, deployment or hardware qualification
is claimed. No live deployment, hardware, or visible-output qualification is
implied by the passing local database suite.
