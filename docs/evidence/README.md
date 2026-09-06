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
