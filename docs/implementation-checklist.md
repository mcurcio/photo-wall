# Runnable MVP delivery checklist

Objective: one review-ready PR against `mcurcio/photo-wall`; do not merge. All requirements remain authoritative. Unchecked means incomplete or unverified.

## Current state (2026-09-05 core benchmark)

- [x] Read repository guidance and all canonical documents; main clean at `ae44c77`.
- [x] Fetch origin and isolate `feat/runnable-mvp` worktree; preserve original checkout.
- [x] Resolve initial time/outage/reboot semantics before schemas/storage in [decision 0001](decisions/0001-mvp-time-recovery-and-module-contracts.md).
- [x] Integrate initial independent requirements, contract and registry reviews; fix scoped material findings. Full architecture/integrated correctness reviews remain required.
- [x] Versioned contracts and initial deterministic clock/cache/Runtime/Planner fault tests.
- [ ] PostgreSQL migrations, central application/media worker, clean-checkout launch, operator configuration and health.
- [x] Network enrollment, separate admin authority, persistent Frames/Outputs/retirement; automatic physical PXE discovery remains below.
- [ ] Preview/commit/revert calibration and two Outputs plus second Player.
- [x] Runtime nested Scenes, fixed roles, independent overlay/calendar boundary, current position reveal, recording Actuators.
- [x] Pure rolling Planner, hard compatibility, locked exact assets, readiness/capacity/commit separation, with PostgreSQL/recording-Player integration.
- [ ] Pinned real Immich/query, evolving candidates during ongoing Run, bounded preparation/delivery.
- [ ] Single Python Player networking/cache/sync/execution/embedded GStreamer/GTK; ordinary continuity.
- [ ] Restart/download/deletion/cache/outage/rejoin fault matrix and scoped update/rollback.
- [ ] Common Pi 5 PXE image build; revision/config/package manifest/checksum outside Git; boot test artifact.
- [ ] Physical fresh PXE registration, replacement, dual Output rendering, continuity, visible coordination.
- [x] Real Immich v2.5.6 adapter fixture with Player-to-Immich DNS and numeric access blocked. Complete worker-to-renderer integration remains a separate gate.
- [ ] CI and reproducible demo; verified clean setup/build/run/test/recovery instructions.
- [ ] Final independent correctness review and verification on final code revision.
- [ ] Publish/update single PR and acceptance evidence; keep draft until gates pass.

## External dependencies requested early

Asked user for two Pi 5 Players, two panels on one Pi, control of a PXE LAN, remote execution and visual capture; no bench details available yet. Docker Desktop runs the central/worker/PostgreSQL and isolated real Immich fixture. Public image pulls use an isolated empty Docker client configuration without changing saved credentials. An isolated external scratch directory has sufficient space for image construction. GitHub read/auth works; user requested one draft PR with benchmark updates. Push is pending exact destination/payload authorization after automatic approval review rejected it. Repository license and security-reporting contact remain owner decisions before a code release.

## Verified core benchmark

- 494 core tests passed together, including real PostgreSQL and a loopback TCP HTTP/WebSocket Player session. Active appliance implementation files were excluded explicitly. Four dependency deprecation warnings remain.
- Central and media worker launch together with healthy PostgreSQL, persistent media storage and private upstream configuration. Operator Source/Scene/Program/Run forms and worker/source health are implemented; API workflow tests pass. Full authenticated browser walkthrough remains pending explicit approval after automatic approval review rejected the disposable localhost token.
- Runtime snapshot recovery, nested Scenes, Program rollover, current-state recording Actuator reveal, required cue membership/deadlines, exact secured assets and current-authority readiness/commit/revocation have regression coverage.
- Single-process Player networking, identity, cache integrity/reacquisition, bounded transfers, clock gating, actual draw acknowledgments and outage/rejoin are integrated with a RecordingRenderer. Native Linux smoke separately verifies JPEG/PNG/H.264, seek, composition/calibration and two distinct Weston outputs. Physical DRM/HDMI and live hotplug remain unqualified.
- Real Immich fixture verifies evolving query results, eight EXIF orientations, permission loss, deletion, outage/recovery, exact originals and network isolation. All 55 preparation tests also pass inside the pinned Trixie Linux worker image, including rotated video and Linux resource limits.
- Signed upstream Ubuntu base download is verified. Common-image/bootstrap and signed A/B update implementation are in progress and excluded from this core checkpoint; no final appliance checksum or boot result exists.

See [the core benchmark evidence](evidence/2026-09-05-core-benchmark.md) for commands, artifact identities, failed attempts and evidence boundaries. Next gates are a clean-checkout verification of this checkpoint, complete real-media network integration, Player-only packaging, signed image/boot/update qualification, physical bench results, and final independent review.

## Evidence rules

Put dated command/revision/result records in `docs/evidence/`. Label each simulated, integration, or physical. A test filename or manifest is not proof it ran. No physical Pi image/boot result exists yet. Never commit credentials, private media, binary images, or unsanitized private server responses.
