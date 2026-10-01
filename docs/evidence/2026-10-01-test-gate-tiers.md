# 2026-10-01 PR test gate: tiers, template databases and the e2e split

Scope: the [runbook's test tiers](../runbook.md#tests-and-local-development) and the
[software e2e](../../.github/workflows/software-e2e.yml) job split, measured on one development
Mac (8 cores) against local Docker PostgreSQL 16.9. **No CI run of these changes exists yet**;
every CI figure below is a projection.

## Before (base 2da99ee)

- CI run 36797558553: `checks / portable-and-postgres` was cancelled at its 30-minute timeout.
  Its Postgres step reported `3927 passed ... in 887.38s`; the browser step then ran 13.8 min
  serially. The e2e job took 6.7 min: 46 s setup, 106 s Immich fixture, 233 s wall scenario.
- Local, serial, `--durations=0`, non-browser suite: 14 min 43 s wall. Summed phases: database
  tests 665 s (setup 432 s, a median 0.46 s per test running the migrations; call 177 s;
  teardown 57 s), unit tests 208 s.

## After (local)

| Tier | Command | Result | Wall |
|---|---|---|---|
| unit | `-m "not db and not browser" -n auto --dist worksteal` | 2992 passed, 22 skipped | 42.7 s |
| db, local Compose database (fsync on) | `-m db -n auto --dist loadgroup` | 942 passed, 3 skipped, 1 xfailed | 104.7 s |
| db, test database (tmpfs), as CI | `scripts/test_local.py -m db -n 4 --dist loadgroup` | 942 passed, 3 skipped, 1 xfailed | 65.7 s |
| browser, test database, as CI | `scripts/test_local.py tests/browser -n 4` | 317 passed; evidence `passed`, 19/19 checks | 109.4 s |

The unit tier's 22 skips are allowlisted or Linux-only on this Mac (none of the Linux-only
reasons appears in run 36797558553's skip list). The database tier's slowest module is
`test_two_pods.py` (one xdist group, its kill-9 rescue alone 46 s).

## Guards, each mutation-probed

- Lock waits scoped to the test's database: removing the auto-promotion advisory lock fails
  `test_v5_the_last_automatic_promotion_read_every_row_committed_before_it` under `-n 4`;
  an unscoped `pg_stat_activity` count in `tests/test_registry.py` fails
  `tests/test_lock_observation_scope.py`.
- Unowned skips fail CI: `tests/test_tier_policy.py` fails when the exit-status line or the `db`
  classification is removed; an unowned skip exits 1 under `CI` (with and without xdist).
- Browser evidence from xdist workers: ignoring worker report facts turns the report from
  `passed` to not-passed, which the CI step refuses.
- Fixture `--setup-only`: running the adapter roles regardless fails its test.
- Collapsed parametrizations keep every case: dropping one demo failure code, or letting one
  repository method accept a fake transaction, fails the collapsed test naming it.

## Projected CI (unconfirmed)

unit ≈ 1.6-1.8 min, db ≈ 1.8-2.2 min, browser ≈ 3.5-4 min, image-smoke ≈ 2.5 min after the plan,
linux-media unchanged (≈ 3.5 min), e2e ≈ 5.6-6.0 min (from 6.7). The e2e job stays the critical
path: a pull request takes ≈ 6.5-6.9 min end to end. The remaining e2e time is the 233 s wall
scenario; reaching under 5 minutes needs a decision recorded in `.claude/errata.md`
(2026-10-01).
