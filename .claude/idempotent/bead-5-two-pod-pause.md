# Bead 5: `two-pod-pause`

**Design:** `docs/central-idempotent-jobs.md`, §5 (first walkthrough), §6 rule 2, §11 (defects 1
and 2).
**Base branch:** `claude/central-followups` with beads 1, 2 and 3 landed.
**Follows:** bead 2 (the behaviour under test) and bead 3 (both edit
`tests/support/github_release.py`). **Packages:** tests only.

## Behaviour under test

A real worker process paused past the 30s liveness limit is rescued, and it is pruned when
another worker starts. When it resumes, its late failure changes nothing, because the copy's data
is already present. This proves issue #26's defects 1 and 2 end to end under the per-data rules.

## Frozen page

- **No production signatures, migrations or codes.**
- Use the existing `TwoPods` harness (`tests/test_two_pods.py`), its fake GitHub origin and
  `spawn_worker`.
- Add one hook to `FakeGitHubOrigin` (`tests/support/github_release.py`): a hold that applies to
  ONE tarball connection (the first after it is armed), and can end that connection with a
  truncated body. Later connections stream normally. The existing all-connections `hold` keeps its
  behaviour.

## Files

`tests/test_two_pods.py`, `tests/support/github_release.py`.

## Existing tests it carries

- `tests/test_two_pods.py:139-142`: the process-health check allows the paused worker to exit with
  any code. Register it as "may exit".
- The module docstring's scenario order (`tests/test_two_pods.py:1-15`): add T7 after A2 and
  describe it.

## Acceptance criteria (T7)

1. Publish a fresh tag in the fake origin and sync it. Arm the one-connection hold, and have a
   request make a worker download the tag's tarball. Identify that worker as A2 does (about
   `tests/test_two_pods.py:389-418`).
2. Send that worker SIGSTOP. After 35s, spawn a new worker (its start prunes the paused worker's
   row), and publish `RescueStalledJobs` on demand.
3. The other worker's copy downloads normally and ends `ok`. Record the outcome row's `seq` and the
   asset's facts.
4. End the held connection with a truncated body, then SIGCONT the paused worker. Its run fails
   with a transient reason (`download_truncated` or `origin_unreachable`).
5. Expect:
   - the outcome row is unchanged (`ok`, the same `seq`);
   - no redelivery row for the key;
   - `GET /v1/netboot/base` serves bytes whose sha256 equals the recorded facts;
   - the file is `os-images/base-<tarball sha>.squashfs`.
6. The paused worker's exit is allowed, not required. It exits only when it wins a fetch (the
   foreign key fails), which is nondeterministic.

## Mutation probe

- M1: skip the presence check in the executor's failure path (T7: the outcome turns `transient`
  and a redelivery row appears).

**Cost:** about 60s more CI.

## Report back

Report the wall time added, and any errata appended where this page is wrong. **Report where the
spec is wrong.**
