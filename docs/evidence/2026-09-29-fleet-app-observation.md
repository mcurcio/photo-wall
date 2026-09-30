# Fleet app observation software evidence — 2026-09-29

**Scope:** PR #39 working tree, F3 installed/running observation and base-agent continuity. This is software evidence, not a Kubernetes deployment or physical Player qualification.

The base agent now sends bounded schema-2 app evidence to Central while preserving its schema-1 heartbeat fallback on a canonical unsupported route. Central uses the same T0 quota, offer correlation and per-boot sequence admission for both versions. Equipment labels installed and running digests as serial claims with receipt age, boot ambiguity and process identity. The claims do not authorize an update or mark an artifact accepted.

Adversarial review found that synchronous root hashing and PID1 sampling could delay OS check-ins and hold the executor lock during activation. The agent now gives app collection a three-second reporting deadline and permits one in-flight collector; late results are discarded. The collector checks short selection snapshots around verification and process sampling, with the slow work outside the mutation lock. The provisioner retries `executor_busy` with the same exact payload and offer, one-second waits and watchdog renewal. The T0 diagnostic recency threshold is 60 seconds, leaving margin over the 10-second period plus local sampling, discovery and two bounded HTTP deadlines during v2-to-v1 fallback. Other executor errors remain terminal. Deterministic tests cover blocked collection, activation during sampling, final-snapshot lock contention, and recovery/activation after a shared-lock refusal.

## Executed checks

- Final focused PostgreSQL and portable set: **251 passed, 1 skipped**. It includes v1/v2 ingress and database admission, delayed old-boot and schema-1 claims, no acceptance write, agent fallback, slow collection, executor contention, provisioner retry, module closure and bootstrapper package unit tests. The skip is the real `dpkg-deb` build/inspection test, which requires Linux.
- Ruff: passed. Documentation relative-link check: 115 Markdown documents checked. `git diff --check`: passed.
- Console production Vite build: passed with the bundled Node v24 runtime.
- A broad local PostgreSQL-backed suite completed during integration with **3,380 passed, 341 skipped, 1 expected failure**. It began before the final short-lock refinement; the focused set above covers that final code. The expected failure is the known legacy media worker lock defect in `test_two_pods.py`. Browser, Linux appliance and optional published-package checks account for many of the skips.

`scripts/test_local.py` was not run in this managed PR worktree because its private `.env` is absent; the PostgreSQL-backed suites used an isolated local test database. Final-revision CI, mixed old/new Central pods behind ingress, a built boot-tree switch-root, a real Pi and visible panel output remain separate gates. No Kubernetes rollout or physical update was performed.
