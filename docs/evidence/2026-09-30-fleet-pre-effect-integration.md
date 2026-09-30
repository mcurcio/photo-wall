# 2026-09-30 fleet pre-effect integration

**Evidence class:** local software and isolated PostgreSQL checks on the PR #39 working tree. **Status:** staged implementation, not a production command, deployment, or physical Player qualification.

The integrated slice adds a default-closed rollout gate, generation- and issuing-session-scoped attempt/acceptance records, a causal applied-control receipt and separate V2 local process proof, drain-derived Runtime admission for manual and queued work, post-lock time cuts, and caller-owned unbound drain/attempt transaction seams. Public bound drain preparation refuses until the due-Program and Run/Actuator policy is selected. T0 boot observations remain separate from authenticated OS command authority. There is no production T1/T2 verifier, command route, stop permit, acceptance writer, per-Output handoff, or rollback gate controller.

## Checks run

- Portable whole repository: `.venv/bin/python -m pytest -q -p no:cacheprovider` using the existing workspace virtualenv, with `PYTHONDONTWRITEBYTECODE=1`: **2,834 passed, 1,157 skipped**, exit 0. Database, browser, Linux-only and physical dependencies account for the environment skips.
- PostgreSQL whole repository: the same pytest command with `PHOTO_WALL_TEST_DATABASE_URL` injected in-process from the already-running local Compose database at `127.0.0.1:54329`: **3,644 passed, 346 skipped, 1 xfailed**, exit 0. Each database fixture used its own temporary schema. The expected failure is the repository's preexisting legacy media-lock case. The direct `scripts/test_local.py -q` invocation could not run in this managed PR worktree because its private `.env` file is absent; the equivalent suite was run with the DSN supplied without writing credentials to the repository.
- `.venv/bin/python -m ruff check .`, `lint-imports --no-cache --no-logo` (six contracts), `python3 scripts/check_docs.py` (119 documents), and `git diff --check`: passed.
- Focused PostgreSQL checks included lock-wait ACK expiry, fallback-offer expiry, Runtime drain crossing, bound-preparation refusal, and caller-owned attempt rollback. The V2 local proof and Player service suite passed 152 cases with three environment skips before the whole-repository runs.

These checks do not prove every routable or rollback Central image, authenticated PXE identity, resident service cadence on a Pi, local root socket composition, visible Output buffers, or physical calibration. The [command trust and rollout audit](2026-09-30-command-rollout-audit.md) records the live serving topology and remaining deployment gates; the [implementation map](../player-fleet-implementation-map.md) records the unresolved D14, D16 and D17 choices.
