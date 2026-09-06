# Runnable MVP delivery checklist

Objective: one review-ready PR against `mcurcio/photo-wall`; do not merge. [Draft PR #2](https://github.com/mcurcio/photo-wall/pull/2) is open from `feat/runnable-mvp` to `main` and will receive verified benchmark updates. All requirements remain authoritative. Unchecked means incomplete or unverified.

## Current state (2026-09-06 integration benchmarks)

- [x] Read repository guidance and all canonical documents; main clean at `ae44c77`.
- [x] Fetch origin and isolate `feat/runnable-mvp` worktree; preserve original checkout.
- [x] Resolve initial time/outage/reboot semantics before schemas/storage in [decision 0001](decisions/0001-mvp-time-recovery-and-module-contracts.md).
- [x] Integrate initial independent requirements, contract and registry reviews; fix scoped material findings. Full architecture/integrated correctness reviews remain required.
- [x] Versioned contracts and initial deterministic clock/cache/Runtime/Planner fault tests.
- [x] Clean-checkout dependency install, central/worker/database image builds and healthy startup; complete 494-test suite on committed `dda8e98`.
- [x] PostgreSQL migrations, central application/media worker, clean-checkout launch, operator configuration and health; authenticated browser walkthrough remains separate.
- [x] Network enrollment, separate admin authority, persistent Frames/Outputs/retirement; automatic physical PXE discovery remains below.
- [x] Preview/commit/revert calibration, two Outputs and a second Player are covered by service and demo integration; physical display qualification remains separate.
- [x] Runtime nested Scenes, fixed roles, independent overlay/calendar boundary, current position reveal, recording Actuators.
- [x] Pure rolling Planner, hard compatibility, locked exact assets, readiness/capacity/commit separation, with PostgreSQL/recording-Player integration.
- [x] Pinned real Immich/query, evolving candidates during ongoing Run, bounded preparation/delivery; full two-Player/three-Output network demo passed with simulated actuation.
- [x] Single-process Python Player networking, cache/sync/execution, embedded GStreamer/GTK integration and ordinary continuity; native full-image and physical rendering remain separate.
- [x] Restart/download/deletion/cache/outage/rejoin fault matrix and scoped update/rollback contracts are tested, including actual Linux systemd acceptance/recovery scenarios; actual image rollback remains pending.
- [x] Common Pi 5 image and PXE tree built; revision/config/package manifest/checksum retained outside Git.
- [x] Boot-test the checksum-identified appliance artifact in the generic ARM64 VM: hosted enrollment, power-cycle identity and central recovery passed at `1eb16ef` and the later hosted exact-image run; physical/native gates remain separate.
- [ ] Authenticated operator browser walkthrough, including configuration and calibration.
- [ ] Native initialization and sustained-health acceptance on the exact hosted image; committed native media and actual automatic image rollback.
- [ ] Physical fresh PXE registration, replacement, dual Output rendering, continuity, visible coordination.
- [x] Real Immich v2.5.6 adapter fixture with Player-to-Immich DNS and numeric access blocked. Complete worker-to-renderer integration remains a separate gate.
- [x] Reproducible CI and full-media demo workflows with retained setup/build/run/test/recovery instructions; standard checks for `3f88b33` passed. Hosted native/rollback qualification remains incomplete.
- [ ] Final independent correctness review and verification on final code revision.
- [ ] Publish/update single PR and acceptance evidence; keep draft until gates pass.

## External dependencies requested early

Asked user for two Pi 5 Players, two panels on one Pi, control of a PXE LAN, remote execution and visual capture; no bench details available yet. Docker Desktop runs the central/worker/PostgreSQL and isolated real Immich fixture. Public image pulls use an isolated empty Docker client configuration without changing saved credentials. An isolated external scratch directory has sufficient space for image construction. GitHub authentication works; explicit user authorization resolved the automatic push-approval gate, and draft PR #2 is open. Feature-branch pushes and PR updates are authorized; main is protected and merging requires the owner. Repository license and security-reporting contact remain owner decisions before a code release.

Earlier bounded Spark work, including the full-demo evidence review, remains valid. Spark is currently unavailable because its context window failed; this is a context limitation, not a quota report. Keep integrated design and correctness with a capable owner until it is available again.

## Verified core benchmark

- 494 core tests passed together, including real PostgreSQL and a loopback TCP HTTP/WebSocket Player session. Active appliance implementation files were excluded explicitly. Four dependency deprecation warnings remain.
- Central and media worker launch together with healthy PostgreSQL, persistent media storage and private upstream configuration. Operator Source/Scene/Program/Run forms and worker/source health are implemented; API workflow tests pass. Full authenticated browser walkthrough remains pending explicit approval after automatic approval review rejected the disposable localhost token.
- Runtime snapshot recovery, nested Scenes, Program rollover, current-state recording Actuator reveal, required cue membership/deadlines, exact secured assets and current-authority readiness/commit/revocation have regression coverage.
- Single-process Player networking, identity, cache integrity/reacquisition, bounded transfers, clock gating, actual draw acknowledgments and outage/rejoin are integrated with a RecordingRenderer. Native Linux smoke separately verifies JPEG/PNG/H.264, seek, composition/calibration and two distinct Weston outputs. Physical DRM/HDMI and live hotplug remain unqualified.
- Real Immich fixture verifies evolving query results, eight EXIF orientations, permission loss, deletion, outage/recovery, exact originals and network isolation. All 55 preparation tests also pass inside the pinned Trixie Linux worker image, including rotated video and Linux resource limits.
- Signed upstream Ubuntu base download is verified. Player-only offline packaging and the signed A/B slot store now have separate tested benchmarks: 64 packaging tests plus an actual ARM64 install, and 62 updater/Release tests plus a root-owned Linux staging/fallback smoke. The final signed image and common PXE tree have verified checksums; subsequent hosted exact-image evidence records generic-VM boot qualification for the later image line, while physical qualification remains pending.

See [the core benchmark evidence](evidence/2026-09-05-core-benchmark.md) for its commands, identities and limits.

## Current acceptance checkpoints

The [full wall evidence](evidence/2026-09-05-full-wall.md) records the current
core/Player revision `1ec354e` full-media pass with **410 final
Readiness-to-Commit checks**. Exact secured deletion is now bound to the
selected Player, epoch, Output, assignment, Run and variant bytes. The command
uses simulated actuation and Outputs; native full-image and physical rendering
remain separate gates. The latest corrected harness validation recorded
**865 passed / 15 explicit skips / 4 dependency warnings** at the latest
`387d2e2` harness revision; detailed historical
attempts and limits remain in the dated evidence.

The signed checkpoint at source `073f57d` has a matching Player-only bundle,
authenticated image/PXE identities and **138 actual Linux appliance checks**;
see [appliance image evidence](evidence/2026-09-05-appliance-image.md). Hosted
run 34020566014 then built and booted the exact signed image from the later
`d9f656b` source line: assembly took **10m13s versus 22m33s cold**, and generic
VM enrollment, power-cycle identity and central rejoin passed. See the
[hosted image evidence](evidence/2026-09-05-github-image.md#second-hosted-exact-image-boot-pass).

Run 34023366287 built, reopened and boot-tested the exact signed image for
`1ec354e` after **9m18s** assembly, about **59% less** than the recorded 22m33s
cold assembly, and published the image and e2e report artifacts. All five
generic-VM checks passed; healthy-trial acceptance, native rendering, physical
Pi/PXE, HDMI and automatic rollback remain false. Standard checks run
34024871867 for `387d2e2` also passed **824 tests / 56 explicit skips / 4
warnings**, followed by **55 pinned Linux media tests**. Its redundant image
run 34024871863 was canceled after the native-trial revision queued; that change
contained only demo/evidence files. It supplies no image qualification.

Five actual Linux systemd acceptance/recovery scenarios pass with production
30/180-second health timing and a recorded recovery action. Actual image
rollback, native healthy-trial acceptance and physical qualification remain
separate; see [update-service evidence](evidence/2026-09-06-systemd-updates.md)
and [health evidence](evidence/2026-09-06-central-health.md).

The remaining acceptance gates are an authenticated operator browser
walkthrough, full-image native health, actual image rollback, fresh physical
Pi PXE/registration/replacement, dual HDMI continuity and visible
coordination, final independent review, and release-owner decisions. The PR
remains draft.

## Evidence rules

Put dated command/revision/result records in `docs/evidence/`. Label each simulated, integration, or physical. A test filename or manifest is not proof it ran. No physical Pi image/boot result exists yet. Never commit credentials, private media, binary images, or unsanitized private server responses.

## Subsequent review and native-health checkpoint

The final integrated core review found a Player-restart content-lock defect:
epoch rotation could reroll secured bytes after a source change. The
[correction](evidence/2026-09-06-restart-content.md) preserves exact historical
content under unchanged binding authority while requiring fresh-epoch
readiness/commitments; 22 focused coordination checks passed and independent
review found no residual issue in that scope. The older full-media pass is
not relabeled as a pass for this new core revision.

A [native-health adapter run](evidence/2026-09-06-native-health.md) passed
production 30-second trial acceptance after 30.437s using real GTK/GStreamer
capacity and Player-generated health, with synthetic authority and rootfs.
Full-image native acceptance, actual rollback, authenticated browser QA and
physical qualification remain open.


## Virtual graphics and native trial gate under verification

The next image gate uses a real virtual DRM device, stock Player connector
integration, and generic GPU modules loaded before entering the signed root.
The production acceptance CLI emits a boot-bound event only after its normal
health gate. A pass now also requires that promotion to survive the real VM
power cycle as the same accepted, non-trial slot A. Native media presentation,
automatic rollback and physical fields remain false.

Local validation passed **883 tests / 15 explicit platform or opt-in skips /
four dependency warnings in 104.48 seconds**, including the preceding 112
focused updater/VM checks (one Linux-root-only skip). The retained Linux builder
confirmed the real `6.8.0-139-generic` GPU closure (`virtio_gpu`,
`virtio_dma_buf`) and QEMU's `max_outputs` option. These are preparation checks;
no hosted native-trial pass is claimed. The ownership-separated connector and
module changes received bounded independent review. Full final review remains
required after hosted verification.

Standard CI at `afff7b1` (run 34025872485) passed **842 tests / 56 skips /
four warnings in 58.15 seconds**, plus **55 Linux media tests in 164.89 seconds**.
Its image run 34025872502 completed cold assembly in **21m20s**, enrolled its
Player and then failed native acceptance at the service time limit. No
native-trial pass is claimed. A subsequent four-boot rollback gate
reuses the prepared root for one signed failed
candidate, stages through the production CLI, and requires the production
recovery service's actual fallback reboot. Hosted rollback remains unqualified.
The [rollback preparation checkpoint](evidence/2026-09-06-vm-rollback.md) passed
924 PostgreSQL-backed tests (15 skips/four warnings) and 160 final focused tests
(one Linux-root skip), with bounded independent reviews. The candidate adds
one compression pass while reusing extraction, packages and Player packaging.

Run 34027456271 then built both signed roots in **11m57s**, with a verified
7.68-second base-cache restoration and 155.535-second candidate preparation.
It was canceled after assembly because the superseded acceptance code could
promote stale health after a slow verification. A regression reproduced that
defect against the old source. The corrected ordering verifies first and then
observes fresh health under the same lock; **930 tests / 15 skips / four warnings**
and **131 focused tests / six platform or opt-in skips** pass. Separate
verification limits and public phase diagnostics preserve the original
30-second continuous-health and 180-second health-deadline requirements.
Current hosted acceptance and rollback remain pending.
The corrected source also passed all five actual Linux/systemd adapter scenarios
in 216.70 seconds and bounded independent updater review. The recovery action
was a marker; it does not close the actual image-reboot gate.
