# 0008 delivery ledger

Implementation of the accepted design [0008](0008-generic-image-and-serial-identity.md).
Scope of this push (owner-chosen 2026-09-11): **software baseline + D0 flash `.img` + hardware serial**.
Deferred: T1/T2 transport trust, I1 certificate tier, D1 netboot `Release` re-freeze, GHA publish.

Land loop per `~/.claude/skills/implementation-workflow/SKILL.md`: implement → verify (full PG gate) → review (high-risk only) → land. One bead at a time in this checkout. Budget: 90 min / 8 agents per bead.

## Milestones & beads

| Bead | Milestone | Package(s) | Risk | Status | SHA |
|---|---|---|---|---|---|
| (design record) | — | docs | — | closed | c976e48 |
| m1-central-lifecycle | M1 central lifecycle (TRACER) | central | authz — reviewed | closed | 4016e43 |
| m2-player-discovery | M2 discovery | player | authz/transport — reviewed | closed | 5c0de40 |
| m2-mdns-browse | M2 discovery | player (+dep) | medium | closed | a8094ab |
| m2-mdns-advertise | M2 discovery | central | low | closed | 58f2448 |
| m3-hardware-serial | M3 flash image | player | medium | closed | ee0396d |
| m3-central-d0-enroll | M3 flash image (tracer completion) | contracts+player+central | authz trust-boundary — reviewed | closed | ad876b5 |
| m3-flash-image | M3 flash image | appliance | infra — CI/hardware gate | landed (CI pending) | 5241f6e |
| baseline-docs | all | docs | low | closed | 6374db8 |
| ci-fixes-1 | integration | player+central+tests | boot-path | closed | a12a79d |

Status values: open · in_progress · blocked (wip branch) · closed.

Docs consolidated into one `baseline-docs` bead (0008 step 7: single flash-and-go
rewrite of runbook / module-pxe-service / README). Code beads never block on it,
but it MUST land before the baseline PR merges (implementation-workflow §3.7).

**baseline-docs must-cover items (found during delivery):**
- Port coupling: central advertises `PHOTO_WALL_HTTP_PORT` (default 8000). A deployment
  that changes the served port (Dockerfile `--port` / compose `PHOTO_WALL_PORT`) MUST
  also set `PHOTO_WALL_HTTP_PORT` or players discover a dead port. (verifier finding, m2-mdns-advertise)
- `PHOTO_WALL_MDNS_ADVERTISE=false` disables advertising (for fully-explicit-config deployments).
- `docs/module-player-service.md:13,:33` claims boot-context `persistence` is "always volatile" —
  now false for flashed D0 players (`persistent`). Update it. (m3-hardware-serial review finding)

## Log
- **M1 (tracer) landed 2026-09-11.** `Registry.unbind` + `DELETE .../binding` +
  `PlayerInventory.is_bound` pending view. Full PG gate green (1493 passed);
  adversarial authz review clean; verifier confirmed 3 mutation probes bite +
  a session-survives-unbind regression test. Errata: 2 entries (epoch-vs-generation
  wording; `is_bound` defaulted non-required) — both benign, see `.claude/errata.md`.
- **First CI run on PR #3 red on all three workflows; fixed in `a12a79d`.** (1) appliance
  `prepare_image` smoke-test pulled `zeroconf` via a module-top import → made lazy; (2) mDNS
  unit tests discovered the real compose central on CI's Docker bridge → unique per-test
  service types; (3) e2e startup timed out because central awaited mDNS advertising before
  readiness → moved to a background task. All local gates green (1538 passed); re-running CI.

- **CI cycle 2 (`9eea67a`), root-caused from artifact data (no guessing).** Second CI run:
  e2e went green (background-advertise fix); MVP had 1 residual flake; appliance still red at
  `prepare_image`. Downloaded the failed run's `pip-install.log` diagnostics artifact →
  proved the appliance wheelhouse omitted `zeroconf`/`ifaddr` (16 wheels, neither present):
  `scripts/build_player.py` `ROOTS` allowlist was never updated for the new dep, so
  `locked_runtime` silently dropped it. Fixed ROOTS. Made `appliance/build.py` `run()` emit
  the failing tool's output tail (was swallowed) so future build failures are diagnosable in
  the GHA log. Also made `test_late_discovery` deterministic (reproduced the shutdown race
  offline; 30/30 green).

- **CI cycle 3 (`9eea67a`→fix): MVP green, appliance root-caused via the new diagnostic.**
  MVP checks passed (late-discovery + mDNS isolation fixed). The `run()` diagnostic printed the
  real appliance error: `appliance/bootstrap.py` imports `contracts.equipment`, but the rootfs
  copy allowlist (`build.py:763`) and the source inventory (`:440`) didn't include it → added
  `equipment` to both. Same allowlist-not-updated class as the ROOTS bug. (e2e that cycle failed
  on an unrelated infra hiccup — Immich fixture container `docker_command_failed` — retrigger.)

## Residuals (follow-up, not blocking)
- Health-JSON `persistence` (`player/service.py` `_write_health`) is hardcoded `"volatile"`
  even for a D0/persistent player — telemetry-only inaccuracy; docs describe actual behavior.

## Errata
Append-only spec contradictions found during implementation live in `.claude/errata.md`.
