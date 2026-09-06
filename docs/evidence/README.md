# Acceptance evidence

Evidence classes are separate:

- **Simulated:** deterministic domain, recording Actuator, protocol and cache fault tests.
- **Integration:** actual database, HTTP service, packaged launch, real declared-version Immich and network-isolation tests when run.
- **Physical:** checksum-identified appliance boot, fresh Pi PXE, HDMI rendering/continuity and visible coordination on named equipment.

Real Immich and native Linux integration have executed evidence; physical Pi/PXE evidence remains pending. Container images for central services are not the required Pi appliance artifact. Keep binary artifacts and private deployment data outside Git. Detailed dated records identify revisions, executed commands, failures and limits; the [delivery checklist](../implementation-checklist.md) tracks remaining acceptance.

- [2026-09-05 foundation](2026-09-05-foundation.md): clean-checkout central launch and 94 passing portable/PostgreSQL tests.
- [2026-09-05 core benchmark](2026-09-05-core-benchmark.md): 494 core tests, real Immich isolation, pinned Linux conversion, native two-output routing and released Player service integration; appliance and physical gates remain open.
- [2026-09-05 packaging and update store](2026-09-05-package-and-update.md): offline Player-only ARM64 install, 64 package tests, and 62 updater/Release tests with Linux signature/staging/fallback smoke; complete image/boot integration remains open.
- [2026-09-05 full wall](2026-09-05-full-wall.md): uninterrupted real-media network demo with two Players, three simulated Outputs, live source changes, secured deletion, outages and rejoin.

- [2026-09-05 appliance image](2026-09-05-appliance-image.md): signed Pi 5 image and common PXE tree built, reopened and copied with verified checksums; 138 Linux appliance checks passed, boot and physical qualification pending.
- [2026-09-05 appliance boot](2026-09-05-appliance-boot.md): signed root mounted with durable state in a generic VM; service startup failed because of OverlayFS root traversal permissions. The correction requires a new CI-built artifact and automated boot qualification.
- [2026-09-05 GitHub image build](2026-09-05-github-image.md): hosted signed image passed generic-VM enrollment, power-cycle identity continuity and central rejoin; downloadable artifact and exact hashes recorded. Physical qualification remains pending.
- [2026-09-05 CI cache checks](2026-09-05-ci-cache.md): verified metadata-preserving base and container caches; measured warm assembly at 11m11s versus 22m15s cold.
- [2026-09-06 systemd updates](2026-09-06-systemd-updates.md): five actual Linux acceptance/recovery adapter scenarios passed; reboot action recorded, with native health and physical rollback still unqualified.
- [2026-09-06 central health](2026-09-06-central-health.md): scheduler errors and stale coordination now affect service health; 840 PostgreSQL-backed tests passed.
