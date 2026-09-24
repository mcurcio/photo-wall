# Bead 4: `two-pod-pause`

**Design:** `docs/central-idempotent-jobs.md`: §5 walkthrough and §11 (defects 1 and 2).
**Base branch:** `claude/central-followups` with bead 1 landed.
**Follows:** bead 1. It is green only once results are ordered by start. It may run in parallel
with bead 2 in its own worktree (disjoint files).
**Packages:** tests only.

## Behaviour under test

A real worker process paused past the 30s liveness limit is rescued. It is also pruned when another
worker starts. When it resumes, its late failure does not replace the copy's `ok`. This proves
issue #26's defects 1 and 1b, and defect 2, end to end.

## Frozen page

- **No production signatures, migration or codes.**
- The test uses the existing `TwoPods` harness (`tests/test_two_pods.py`), its fake GitHub origin,
  and `spawn_worker`.
- If aborting a held tarball body needs a hook, add it to `tests/support/github_release.py`.

## Files

`tests/test_two_pods.py`; `tests/support/github_release.py`, only if the abort hook is needed.

## Existing tests it carries

- `tests/test_two_pods.py:139-142`: the process-health check must allow the paused worker to exit
  with any code. Register it as "may exit".
- Scenario order in the module docstring (`tests/test_two_pods.py:1-15`): add T7 after A2 and
  document it.

## Acceptance criteria (T7)

1. A fresh tag is published in the fake origin and synced. Its tarball is held mid-body, and a
   request makes a worker download it. Identify that worker the way A2 does
   (`tests/test_two_pods.py:389-418`).
2. Send that worker SIGSTOP. After 35s, spawn a new worker (its start prunes the paused worker's
   record), and publish `RescueStalledJobs` on demand.
3. The other worker's copy downloads normally and ends `ok`. Record the outcome row
   (`start_number`, `seq`).
4. Abort the held body, then SIGCONT the paused worker. Its run fails.
5. Expect:
   - the outcome row is unchanged (`ok`, the copy's `start_number` and the same `seq`);
   - no second NOTIFY and no redelivery row;
   - `GET /v1/netboot/base` serves bytes whose sha256 equals the recorded facts.
6. The paused worker's exit is allowed, not required. It exits only when it wins a fetch (the
   foreign key fails), which is nondeterministic.

## Mutation probe

- M1: revert `JobOutcomes.record` to the unconditional upsert (T7 red: the outcome turns failed).

**Cost:** about 60s more CI.

## Report back

Report the wall time added, and any errata appended where this page is wrong.
