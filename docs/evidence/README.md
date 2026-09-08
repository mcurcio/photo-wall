# Acceptance evidence

- [2026-09-08 CI package acquisition](2026-09-08-ci-package-acquisition.md): two
  image-construction download timeouts, exact public diagnostic archive hashes,
  and the boundary between CI dependencies and immutable PXE delivery.

Evidence classes are separate:

- **Simulated:** deterministic domain, recording Actuator, protocol and cache fault tests.
- **Integration:** actual database, HTTP service, packaged launch, real declared-version Immich and network-isolation tests when run.
- **Appliance:** checksum-identified signed image/PXE tree inspection and hosted generic-VM boot, with native full-image health and actual image rollback tracked separately.
- **Physical:** fresh Pi PXE, HDMI rendering/continuity and visible coordination on named equipment.

Real Immich, native Linux and hosted generic-VM integration have executed evidence; physical Pi/PXE evidence remains pending. Container images for central services are not the required Pi appliance artifact. Keep binary artifacts and private deployment data outside Git. Detailed dated records identify revisions, executed commands, failures and limits; the [delivery checklist](../implementation-checklist.md) tracks remaining acceptance.

[Decision 0006](../decisions/0006-central-authority-and-stateless-players.md) supersedes the earlier durable Player, `PWSTATE`, local A/B, and custom-worker scheduling designs. The records below are intentionally preserved for the exact revisions they exercised. None of those older results qualifies the replacement architecture; current acceptance requires new final-revision evidence.

- [2026-09-05 foundation](2026-09-05-foundation.md): clean-checkout central launch and 94 passing portable/PostgreSQL tests.
- [2026-09-05 core benchmark](2026-09-05-core-benchmark.md): 494 core tests, real Immich isolation, pinned Linux conversion, native two-output routing and released Player service integration; appliance and physical gates remain open.
- [2026-09-05 packaging and update store](2026-09-05-package-and-update.md): offline Player-only ARM64 install, 64 package tests, and 62 updater/Release tests with Linux signature/staging/fallback smoke; complete image/boot integration remains open.
- [2026-09-05 full wall](2026-09-05-full-wall.md): current core/Player `1ec354e` full-media pass with exact secured-deletion proof, two Players, three simulated Outputs, 410 final Readiness-to-Commit checks, outages and rejoin; actuation remains simulated. The latest corrected `387d2e2` harness validation recorded 865 passed / 15 explicit skips / 4 dependency warnings.

- [2026-09-05 appliance image](2026-09-05-appliance-image.md): signed Pi 5 image and common PXE tree built, reopened and copied with verified checksums; 138 Linux appliance checks passed. Later hosted exact-image boot evidence is recorded separately; physical qualification remains pending.
- [2026-09-05 appliance boot](2026-09-05-appliance-boot.md): historical signed-root generic-VM startup failure from OverlayFS root traversal permissions, retained alongside the later RuntimeDirectory and OverlayFS corrections and successful hosted generic-VM evidence.
- [2026-09-05 GitHub image build](2026-09-05-github-image.md): hosted signed images passed generic-VM enrollment, power-cycle identity continuity and central rejoin; the third hosted pass records exact artifact hashes and 9m18s cached assembly versus 22m33s cold. The later `afff7b1` image enrolled but failed native acceptance at its service limit. Standard checks at `3f88b33` passed 883 tests / 56 skips / four warnings plus 55 Linux media tests. Physical/native/rollback qualification remains pending.
- [2026-09-05 CI cache checks](2026-09-05-ci-cache.md): verified metadata-preserving base and container caches; measured warm assembly at 11m11s versus 22m15s cold.
- [2026-09-06 Player clock sampling](2026-09-06-player-clock.md): local parsing no longer inflates transport uncertainty; 72 offline service tests and a before/after mutation check pass, with one explicit database skip. Hosted qualification remains pending.
- [2026-09-06 Frame and calendar acceptance](2026-09-06-core-acceptance.md): independent 146-test PostgreSQL review and a repeatable nested-calendar RecordingActuator trace; physical and whole-MVP acceptance remain separate.
- [2026-09-07 central authority/stateless Player review](../reviews/central-stateless-review.md): independent working-tree architecture/correctness review, material-finding fixes, and verification boundaries; final-revision image and physical acceptance remain pending.
- [2026-09-06 exact-VM media preparation](2026-09-06-vm-media.md): real media-enabled boot services, production worker source refresh, read-only tiny-filesystem cache probes and strict presentation-evidence joins; full-image photo/rollback qualification remains pending.
- [2026-09-06 systemd updates](2026-09-06-systemd-updates.md): five actual Linux acceptance/recovery adapter scenarios passed with a recorded recovery action; actual image rollback and native/physical qualification remain unqualified.
- [2026-09-06 central health](2026-09-06-central-health.md): scheduler errors and stale coordination now affect service health; 840 PostgreSQL-backed tests passed.

- [2026-09-06 restart content](2026-09-06-restart-content.md): same-key epoch changes preserve secured bytes while requiring fresh readiness and commitments.
- [2026-09-06 native health](2026-09-06-native-health.md): actual native Player health promoted a signed synthetic trial after 30.437s; physical/full-image acceptance remains separate.
- [2026-09-06 VM rollback preparation](2026-09-06-vm-rollback.md): both signed roots assembled in GitHub in 11m57s; the superseded boot run was canceled. A reproduced stale-health acceptance defect is corrected, with 930 regression and 131 focused tests. Actual rollback still requires hosted execution.
