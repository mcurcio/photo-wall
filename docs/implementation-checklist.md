# Runnable MVP delivery checklist

Objective: one review-ready PR against `mcurcio/photo-wall`; do not merge. [Draft PR #2](https://github.com/mcurcio/photo-wall/pull/2) is open from `feat/runnable-mvp` to `main` and will receive verified benchmark updates. All requirements remain authoritative. Unchecked means incomplete or unverified.

## Current state (2026-09-05 integration benchmarks)

- [x] Read repository guidance and all canonical documents; main clean at `ae44c77`.
- [x] Fetch origin and isolate `feat/runnable-mvp` worktree; preserve original checkout.
- [x] Resolve initial time/outage/reboot semantics before schemas/storage in [decision 0001](decisions/0001-mvp-time-recovery-and-module-contracts.md).
- [x] Integrate initial independent requirements, contract and registry reviews; fix scoped material findings. Full architecture/integrated correctness reviews remain required.
- [x] Versioned contracts and initial deterministic clock/cache/Runtime/Planner fault tests.
- [x] Clean-checkout dependency install, central/worker/database image builds and healthy startup; complete 494-test suite on committed `dda8e98`.
- [ ] PostgreSQL migrations, central application/media worker, clean-checkout launch, operator configuration and health.
- [x] Network enrollment, separate admin authority, persistent Frames/Outputs/retirement; automatic physical PXE discovery remains below.
- [ ] Preview/commit/revert calibration and two Outputs plus second Player.
- [x] Runtime nested Scenes, fixed roles, independent overlay/calendar boundary, current position reveal, recording Actuators.
- [x] Pure rolling Planner, hard compatibility, locked exact assets, readiness/capacity/commit separation, with PostgreSQL/recording-Player integration.
- [x] Pinned real Immich/query, evolving candidates during ongoing Run, bounded preparation/delivery; full two-Player/three-Output network demo passed with simulated actuation.
- [ ] Single Python Player networking/cache/sync/execution/embedded GStreamer/GTK; ordinary continuity.
- [ ] Restart/download/deletion/cache/outage/rejoin fault matrix and scoped update/rollback.
- [x] Common Pi 5 image and PXE tree built; revision/config/package manifest/checksum retained outside Git.
- [ ] Boot-test the checksum-identified appliance artifact.
- [ ] Physical fresh PXE registration, replacement, dual Output rendering, continuity, visible coordination.
- [x] Real Immich v2.5.6 adapter fixture with Player-to-Immich DNS and numeric access blocked. Complete worker-to-renderer integration remains a separate gate.
- [ ] CI and reproducible demo; verified clean setup/build/run/test/recovery instructions.
- [ ] Final independent correctness review and verification on final code revision.
- [ ] Publish/update single PR and acceptance evidence; keep draft until gates pass.

## External dependencies requested early

Asked user for two Pi 5 Players, two panels on one Pi, control of a PXE LAN, remote execution and visual capture; no bench details available yet. Docker Desktop runs the central/worker/PostgreSQL and isolated real Immich fixture. Public image pulls use an isolated empty Docker client configuration without changing saved credentials. An isolated external scratch directory has sufficient space for image construction. GitHub authentication works; explicit user authorization resolved the automatic push-approval gate, and draft PR #2 is open. Feature-branch pushes and PR updates are authorized; main is protected and merging requires the owner. Repository license and security-reporting contact remain owner decisions before a code release.

The user reaffirmed `gpt-5.3-codex-spark` for appropriate bounded leaves. It became available during this task and completed the full-demo evidence review and documentation work. Its five-hour usage pool subsequently reached 100%; the reported reset is 2026-09-06 at 00:42 Pacific. Use supported fallback agents while that pool is unavailable, then prefer Spark again for suitable leaves. Integrated design and correctness review remain with a capable owner; no fallback is described as Spark.

## Verified core benchmark

- 494 core tests passed together, including real PostgreSQL and a loopback TCP HTTP/WebSocket Player session. Active appliance implementation files were excluded explicitly. Four dependency deprecation warnings remain.
- Central and media worker launch together with healthy PostgreSQL, persistent media storage and private upstream configuration. Operator Source/Scene/Program/Run forms and worker/source health are implemented; API workflow tests pass. Full authenticated browser walkthrough remains pending explicit approval after automatic approval review rejected the disposable localhost token.
- Runtime snapshot recovery, nested Scenes, Program rollover, current-state recording Actuator reveal, required cue membership/deadlines, exact secured assets and current-authority readiness/commit/revocation have regression coverage.
- Single-process Player networking, identity, cache integrity/reacquisition, bounded transfers, clock gating, actual draw acknowledgments and outage/rejoin are integrated with a RecordingRenderer. Native Linux smoke separately verifies JPEG/PNG/H.264, seek, composition/calibration and two distinct Weston outputs. Physical DRM/HDMI and live hotplug remain unqualified.
- Real Immich fixture verifies evolving query results, eight EXIF orientations, permission loss, deletion, outage/recovery, exact originals and network isolation. All 55 preparation tests also pass inside the pinned Trixie Linux worker image, including rotated video and Linux resource limits.
- Signed upstream Ubuntu base download is verified. Player-only offline packaging and the signed A/B slot store now have separate tested benchmarks: 64 packaging tests plus an actual ARM64 install, and 62 updater/Release tests plus a root-owned Linux staging/fallback smoke. The final signed image and common PXE tree now have verified checksums; boot qualification remains pending.

See [the core benchmark evidence](evidence/2026-09-05-core-benchmark.md) for its commands, identities and limits.

## Full wall and appliance checkpoints

The [full wall demo](evidence/2026-09-05-full-wall.md) passed one uninterrupted command with two Players, three Outputs, five prepared variants and 485 final Readiness-to-Commit checks. Both live uploads appeared during the same Run. Secured deletion, permission/upstream recovery, fallback beyond central lease expiry, recovery on every Output, same-key/new-epoch rejoin and final upstream network denials passed. Actuation remained simulated; native rendering and physical measurements are separate gates.

Appliance source checkpoint `073f57d7547507de452a2fd273088d7542a6d070` has a matching Player-only bundle exported from a clean checkout. [Its CI run](https://github.com/mcurcio/photo-wall/actions/runs/33999952831) passed **643 tests with 48 explicit skips**, then **55 pinned Linux preparation tests**. The skips comprise 46 host conversion cases and two separately enabled Linux image-tooling cases. The signed builder/bootstrap, automatic 30-second trial health acceptance, public release gateway and synthetic time helper are committed. The [final signed image](evidence/2026-09-05-appliance-image.md) and common PXE tree are built, authenticated, reopened and copied with matching hashes. The latest actual Linux appliance suite passed 138 checks. VM boot and physical qualification remain pending. The subsequent full-wall checkpoint `72d74e7` also has [green CI](https://github.com/mcurcio/photo-wall/actions/runs/34007945286).

The current revision-bound full demo also passed at core/Player `901a3a5`: two Players, three simulated Outputs, five variants and 374 final Readiness-to-Commit checks, including all live-change and recovery phases. The preceding retry failed its secured-presentation check; both attempts and the diagnostic limits remain in the [full-wall evidence](evidence/2026-09-05-full-wall.md). The optimized standard GitHub workflow passed 779 checks plus 55 Linux conversion tests. The first hosted Ubuntu cache was published successfully; warm-cache timing and Pi VM boot qualification remain pending.

Next gates are boot/update qualification, the authenticated operator walkthrough, physical bench results and final independent review. The PR remains draft.

## Evidence rules

Put dated command/revision/result records in `docs/evidence/`. Label each simulated, integration, or physical. A test filename or manifest is not proof it ran. No physical Pi image/boot result exists yet. Never commit credentials, private media, binary images, or unsanitized private server responses.
