# Runnable MVP delivery checklist

Objective: make [draft PR #2](https://github.com/mcurcio/photo-wall/pull/2) reviewable without merging it. Unchecked items remain required acceptance gates.

## Stateless-Player refactor

- [x] Preserve earlier evidence and the latest failed-clock report as dated historical records.
- [x] Make PostgreSQL, release records, equipment bindings, and queue state central-only.
- [x] Add transaction-bound Installation and Media interfaces; remove coordination cross-domain table access and private-method calls.
- [x] Add shared typed release, enrollment, time, and execution-outcome models.
- [x] Use Psycopg connection pooling and Import Linter contracts.
- [x] Exclude central, media, appliance, PostgreSQL, Procrastinate, and SQLite dependencies from the Player package.
- [x] Replace retained Player identity, database, execution journal, update slots, and persistent health gates with fresh-session enrollment and central epochs.
- [x] Make the cache a bounded in-memory index over optional temporary files; validate complete size and digest before reuse.
- [x] Preserve central secured selections across Player restart while requiring fresh-session readiness and commitments.
- [x] Move immutable release, accepted/candidate, boot attempt, trial consumption, health, promotion, and rollback selection authority into PostgreSQL.
- [x] Remove the local A/B/PWSTATE implementation and emit a boot-only disk plus PXE tree.
- [x] Replace the custom worker dispatcher, retry timing, polling, and heartbeat loops with Procrastinate 3.9.0 using caller-owned PostgreSQL transactions.
- [x] Separate authenticated `/v1/player/time` probes from state delivery and expose RTT, offset, delay, drift, and rejection diagnostics.
- [x] Centralize container build policy and explicitly select the daemon `default` builder with `--load` for locally loaded parents.
- [x] Trial GStreamer GL behind the Renderer boundary. Required GL factories are absent from the current image closure, so retain the existing GTK3/appsink renderer pending physical evidence.
- [x] Add [decision 0006](decisions/0006-central-authority-and-stateless-players.md) and update current architecture, execution, Player, provisioning, release, queue, demo, validation, and runbook documents.

## Automated verification on the working tree

- [x] Full PostgreSQL-backed suite: 1,047 passed, 15 explicit platform or opt-in skips, four dependency deprecation warnings.
- [x] Demo harness suite: 60 tests collected; its final-revision hosted run remains an acceptance gate below.
- [x] Ruff, Import Linter, documentation-link validation, bytecode compilation, and diff integrity pass.
- [x] Release tests cover immutable registration, default/candidate selection, transactional once-only trials, duplicate request idempotency, central restart, stale health rejection, session binding, promotion, and accepted fallback.
- [x] Player tests cover empty cache, valid survivor reuse, deletion/corruption recovery, fresh credentials, higher epochs, old-session rejection, control outage continuity, clock gates, and exact media delivery.
- [x] Queue tests cover atomic domain-request/defer commit and rollback, bounded Procrastinate retries, publication recovery, reservations, and stale-attempt fencing.
- [x] Image harness tests require a boot-only disk, central tickets, volatile health, candidate watchdog reboot, and centrally selected fallback.
- [x] Candidate bootstrap requires an initramfs-armed nowayout hardware watchdog; systemd takes it over and pre-root failures force reboot.

## Final-revision acceptance

- [ ] Commit the final refactor revision and build its exact Player wheelhouse, central image, worker image, signed appliance image, and PXE tree.
- [ ] Rerun the complete two-Player/three-Output demo on that revision. The current harness refuses uncommitted or mismatched inputs.
- [ ] Complete the authenticated operator-browser walkthrough, including binding and calibration.
- [ ] Boot the exact image without a writable Player volume and with an empty cache; recover recognized equipment bindings, acquire assets, and render committed content.
- [ ] Restart the Player process and machine; prove fresh epochs, old-session rejection, current authority reconstruction, and valid-file reuse without a media request.
- [ ] Delete and corrupt cached files; prove reacquisition without reenrollment or a changed centrally secured selection.
- [ ] Prove duplicate boot request handling, failed-candidate automatic reboot, consumed trial evidence, and centrally selected accepted fallback on the exact image.
- [ ] Qualify physical Pi PXE enrollment, recognized-equipment recovery, explicit replacement binding, dual HDMI, running-process continuity, resource use, and visible coordination.
- [ ] Complete independent final architecture/correctness review and publish evidence tied to the final revision.
- [ ] Keep PR #2 draft until every required acceptance item above passes.

## Evidence policy

Earlier dated A/B, durable-identity, and reboot-cache results remain valid only for the revisions and designs they actually exercised. They do not qualify this replacement architecture. New evidence must record the exact revision and artifact hashes, distinguish simulated/VM/physical results, preserve failed attempts, and exclude credentials, private media, raw images, and unbounded private service output.
