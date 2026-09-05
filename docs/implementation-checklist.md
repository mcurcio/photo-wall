# Runnable MVP delivery checklist

Objective: one review-ready PR against `mcurcio/photo-wall`; do not merge. All requirements remain authoritative. Unchecked means incomplete or unverified.

## Current state (2026-09-05)

- [x] Read repository guidance and all canonical documents; main clean at `ae44c77`.
- [x] Fetch origin and isolate `feat/runnable-mvp` worktree; preserve original checkout.
- [x] Resolve initial time/outage/reboot semantics before schemas/storage in [decision 0001](decisions/0001-mvp-time-recovery-and-module-contracts.md).
- [x] Integrate initial independent requirements, contract and registry reviews; fix scoped material findings. Full architecture/integrated correctness reviews remain required.
- [x] Versioned contracts and initial deterministic clock/cache/Runtime/Planner fault tests.
- [ ] PostgreSQL migrations, central application/media worker, clean-checkout launch, operator configuration and health.
- [ ] Automatic discovery/enrollment, separate admin authority, persistent Frames/Outputs/retirement.
- [ ] Preview/commit/revert calibration and two Outputs plus second Player.
- [ ] Runtime nested Scenes, fixed roles, independent overlay/calendar boundary, current position reveal, recording Actuators.
- [ ] Pure rolling Planner, hard compatibility, locked exact assets, readiness/capacity/commit separation.
- [ ] Pinned real Immich/query, evolving candidates during ongoing Run, bounded preparation/delivery.
- [ ] Single Python Player networking/cache/sync/execution/embedded GStreamer/GTK; ordinary continuity.
- [ ] Restart/download/deletion/cache/outage/rejoin fault matrix and scoped update/rollback.
- [ ] Common Pi 5 PXE image build; revision/config/package manifest/checksum outside Git; boot test artifact.
- [ ] Physical fresh PXE registration, replacement, dual Output rendering, continuity, visible coordination.
- [ ] Real Immich test with Player-to-Immich network access blocked.
- [ ] CI and reproducible demo; verified clean setup/build/run/test/recovery instructions.
- [ ] Final independent correctness review and verification on final code revision.
- [ ] Publish/update single PR and acceptance evidence; keep draft until gates pass.

## External dependencies requested early

Asked user for two Pi 5 Players, two panels on one Pi, control of a PXE LAN, remote execution and visual capture; no bench details available yet. Docker Desktop was started successfully. Public image pulls initially stalled in its credential helper; a temporary empty Docker client config outside the repo enabled public pulls without changing saved credentials. Local PostgreSQL/central services now run. GitHub read/auth works with network escalation; no preexisting open PR found. Code can progress independently. Repository license and security-reporting contact are owner decisions before public code release; draft implementation work can proceed.

## Integrated foundations

- Central registry API + operator forms + real PostgreSQL migration/restart/concurrency tests. Full source/Scene/Program/health workflow remains incomplete.
- Pure Runtime with nested Scenes, Program rollover, current-state Actuator reveal and snapshot recovery; pure bounded Planner with original eligibility, live results, secured byte locks and revisable arbitration. No current execution authority or physical rendering is implied.
- SQLite Player cache with integrity, crash recovery, secure-before-commit pins, bounded acquisition, failed eviction handling and lower-quota recovery.
- Operator browser login/layout loads. Full browser walkthrough is pending explicit approval: automatic approval review rejected use of the public disposable test token on localhost:8010. API tests remain valid evidence for their scope.
- Exact Immich v2.5.6 media design is being prepared independently; implementation and real-service fixture test remain next.

## Evidence rules

Put dated command/revision/result records in `docs/evidence/`. Label each simulated, integration, or physical. A test filename or manifest is not proof it ran. No hardware, real Immich, Pi image identity, or boot result exists yet. Never commit credentials, private media, binary images, or unsanitized private server responses.
