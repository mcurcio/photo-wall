# Validation

This specification maps [requirements](requirements.md) to ownership, evidence, and proposed [implementation slices](implementation-plan.md). Portable and PostgreSQL tests now exist; dated [evidence](evidence/README.md) distinguishes what ran from unqualified appliance/media/physical paths. The stateless-Player refactor requires rerunning the complete two-Player/three-Output demo, authenticated browser walkthrough, exact-image native/cache/rollback paths, and independent review. Physical Pi/PXE/replacement/dual-HDMI/continuity/visible-coordination acceptance is still pending.

## Acceptance matrix

| ID / scenario | Ownership | Evidence | Planned slice |
|---|---|---|---|
| V01 — Fresh PXE startup uses no writable persistent Player volume, creates a fresh session, and recovers central bindings for recognized equipment; unknown equipment remains unbound and replacement requires an explicit binding change. | Central provisioning/registry/sessions; Player equipment; appliance tooling | Empty local state, common image, equipment observations, fresh epoch, central appearance/binding, old-session rejection | S3, S6 |
| V02 — Both Outputs display independent calibrated stills/videos and transitions while ordinary playback retains intended content or fallback. | Player Renderer/surfaces; equipment configuration | Record both panels; preview/commit/revert; whole-Player capacity; boot and crash boundaries reported separately | S1, S3 |
| V03 — Immich-unaware Players receive media exclusively through the central show system; new matches enter ongoing Runs, compatibility holds, and empty pools use valid still/fallback output. | Central catalog/eligibility/Planner/media; Player cache/executor | Upstream access blocked; no upstream details/keys/redirects in configuration/protocol; media and assignment/retention traces | S4 |
| V04 — Acquisition failure yields an eligible replacement or authored alternative/fallback; scheduled and secured assignments retain exact content. | Planner/media/commitment persistence; Player cache | Upstream deletion before/after acquisition, cache pressure after content lock but before commitment, integrity failures, restart around commitment | S2, S4, S6 |
| V05 — Future projection changes no current lifecycle state and emits no premature visual or Actuator cues; readiness includes shared Player capacity. | Runtime/Planner/coordination; Player executor; Actuator adapters | Controllable-clock traces, recording adapter, missing/late participants, whole-Pi capacity checks | S2, S5 |
| V06 — Nested talking portraits retain authored locations; all darkened Frames participate; an omitted lamp receives no implied command. | Definitions/Runtime/Planner; Renderer; Actuator adapter | Authored fixtures, resolved typed participants, physical output and lamp-command trace | S5 |
| V07 — An independent overlay crosses December/January and reveals current background, video, and lighting state without replaying superseded cues. | Programs/Runtime/Planner; Player executor/Renderer; Actuator adapter | Accelerated schedule plus physical reveal; lamp-covered/uncovered cases; opaque black versus opacity fade | S5 |
| V08 — Natural completion waits for own work and children; cancellation is downward only; Program expiry stops new cycles without draining prefetch. | Activation ingress/Runtime; Planner/executor | Deterministic lifecycle traces, including child cancellation and current child-sequence completion | S2, S5 |
| V09 — Repeat activation defaults to ignore; manual activation does not imply force; authored edits and live query changes follow distinct adoption rules. | Activation ingress/definitions/Runtime | Duplicate/overlap fixtures, authored edit during Run, later child launch under selected revision policy | S2, S5 |
| V10 — Process restart and reboot create fresh session authority, reject old grants, preserve central secured assignments, and rebuild disposable readiness. | Durable Runtime/Planner/coordination; stateless Player cache/executor | Process and machine restart, old-session messages, empty/valid/corrupt cache, diagnosed running-process outage limit | S6 |
| V11 — An operator installs/migrates central services, completes authenticated workflows, and recovers a failed candidate through automatic reboot plus centrally selected rollback. | Central packaging/operator interface; registry/releases; appliance tooling | Clean walkthrough, PostgreSQL/Procrastinate startup, staged trial, matching health, watchdog reboot, next-boot accepted release | S3–S6 |
| V12 — Bedtime darkness persists even if the optional goodbye fails; wake-up follows an explicit policy. | Operational controls/Runtime; display/Actuator adapters | Required-outcome/optional-effect failure, persistent state, wake-up | S6 if included |

## Qualification procedures

When an appliance run exposes a failure outside hardware or operating-system integration, first reproduce it in an ordinary automated integration test at the owning subsystem boundary, running outside the appliance OS. Fix and verify it there before rerunning the image; reserve image runs for operating-system, native boot, and rendering integration and final exact-artifact qualification.

### Portable execution checks

Use controllable time, simulated Players, and a recording Actuator adapter. Trace activation IDs, Run epochs, authored/plan revisions, assignments, binding generations, intended times, and observed execution. Keep checks runnable without Pi hardware; follow [CONTRIBUTING](../CONTRIBUTING.md).

Project across a calendar boundary before advancing current time. Assert that projection does not start children, complete the current Run, or command devices. Then advance time through preparation and commitment. Inject repeated, stale, reordered, missing, and late messages; missing required/optional participants; insufficient aggregate decoder capacity; and failures affecting completion. Check feedback against the [execution contract](execution-contract.md).

Exercise a parent whose later child owns additional targets: membership alone must not darken those targets early. Test overlapping activation requests under the selected policy, cancellation at each tree level, expiry with prefetched future work, and authored changes before a later child launch.

Validate the complete Installation HTTP response with Players, nested Outputs, and bound/calibrated Frames. Reject malformed nested data even when its Player projection would be valid. Exercise enrollment observation before health or Output feedback arrives: an empty inventory and an unchanged/older epoch remain `pending`; a matching Equipment at a strictly newer epoch becomes `ready` with a session. Reject inconsistent state/session pairs, retired or mismatched Equipment, and prove pending and ready evidence can both be serialized to JSON. These checks establish the [enrollment observation boundary](module-appliance-e2e.md#enrollment-observation-boundary), not release-health or presentation acceptance.

### Physical Player and timing

1. Pin the Pi OS/kernel, graphics stack, GStreamer/plugins, GTK/PyGObject, event-loop arrangement, and appliance configuration. Record Pi/panel models, output modes, network, storage, cooling, and media profiles.
2. On one Pi with two representative panels, run stills and representative HEVC/H.264 video through real transforms, photometric correction, transitions, and overlays. Measure both Outputs together: CPU/GPU load, memory, thermals/throttling, decoder use, dropped/late frames, file bandwidth, and preparation latency.
3. Cover a playing clip, suspend unnecessary hidden decode, advance logical time, then prepare and reveal its current assignment/offset. Record seek/preroll cost and resource peaks. Cached files alone do not prove transition readiness; paused media time is not logical Run time.
4. Record both panels through media replacement, end-of-stream, invalid/missing media, pipeline recovery, Player crash, cold boot, and display-host recovery. Separate ordinary continuity, fatal-process fallback, signal interruptions, and monitor-generated messages. A persistent window alone does not prove retention of a valid picture.
5. Add a second Player for repeated simultaneous markers and sustained playback. Record intended and agent-observed presentation times alongside camera/photodiode measurements of visible onset and drift. Include capture resolution, sample count, and measurement uncertainty; clock agreement does not establish panel synchronization.

Dual-output viability permits appliance integration during cross-Player measurement. Coordinated playback claims require completed physical qualification on the claimed configuration; software timestamps establish neither genlock nor frame accuracy.

### Media integration and recovery

Use a real Immich instance with recorded version, permissions, and fixtures: eligible portrait/landscape media, landscape photos excluded from portrait Frames, a 720p original excluded from a 40-inch Frame, insufficient originals with larger derivatives, and spatially authored alternatives. Distinguish empty eligibility from API incompatibility, denied permission, and transport failure.

Block direct Player-to-Immich traffic while allowing central upstream access. With empty Player caches, verify acquisition, variant preparation, playback, and recovery exclusively through the central show system. Inspect Player code, packaged dependencies, configuration, manifests/control messages, and request traces: no Immich-specific SDK or integration logic, Immich credentials, upstream API details, direct upstream calls, redirects to Immich, PostgreSQL driver, or Procrastinate dependency.

During an ongoing Run, add matching media and remove assets before acquisition and after securing an assignment. Inject partial/corrupt downloads, unsupported media, slow/failed conversion, and storage pressure. Verify exact variant identity, fixed committed content, compatible fallback, and bounded retention; a long-lived Run must not pin all past media.

For initial discovery and each upstream mutation, permission change, or network fault/recovery, retain an authenticated [refresh request receipt](module-media-worker.md#source-refresh-requests). Wait for the same Source's completed revision to reach that requested revision, then inspect the expected status and exact media/assignment effects. Test enqueue rollback, duplicate/coalesced requests, requests arriving during an active lease, central/worker restart, and periodic/explicit refresh races. A stale lease must not publish over its replacement or complete newer requests; an unrelated Source and a pre-request snapshot must not satisfy the wait. A completed failure remains a failed source observation. Keep periodic refresh enabled and qualify its normal live convergence separately.

Restart central and Player components around acquisition, readiness, commitment, and presentation. A Player process restart must create a fresh key/epoch and reconstruct current authority from central. Delete the cache and repeat from empty; corrupt a selected file and verify exact reacquisition without a changed central selection. When valid content-addressed files survive, verify complete validation and reuse without a media request. Continue a running-process outage through the tested lease and reconnect; record where authorization ends. Do not infer cold-reboot playback without central connectivity.

Restart central around domain-request/task-defer, preparation publication, and release selection. Verify the domain request and Procrastinate job commit atomically, stalled/retried work keeps the stale-attempt fence, and publication recovery remains correct. Exercise duplicate boot requests, consumed trials, stale health, and current-session binding. For rollback, boot a real candidate artifact, prevent sustained health, observe the real automatic reboot, and prove the next PXE request selects and renders the centrally accepted release with no local update state.

### Operator and integration checks

1. In a clean environment, use published commands to launch central storage, discovery, provisioning, and registration services and apply migrations. Configure shared services centrally.
2. Connect and power a fresh, supported network-boot-capable device with no writable persistent Player volume, prior enrollment, saved configuration, endpoint, credentials, cache, or Frame assignment. Verify PXE delivery, central release ticket/rootfs selection, fresh-session enrollment, and automatic central appearance. Then independently wait for Outputs and health before Frame assignment; delayed feedback must not invalidate an already observed enrollment. Use no SSH, per-device configuration, endpoint entry, or manual credential provisioning.
3. Repeat with another fresh device. Record identical image identity and automatic registration; no individualized image is permitted.
4. Centrally identify Outputs, bind existing Frames, calibrate, and configure source/Scene/Program playback. Reboot recognized equipment and verify its fresh session regains central bindings. Connect replacement equipment and verify it remains unbound until an explicit operator change. Reject old-session grants. Exercise candidate failure, automatic reboot, and centrally selected rollback without reconstructing Frames or using local update state.

Where included, test stable Home Assistant entity IDs, restart/reconnection, discovery/state reannouncement, request idempotency, and logical Actuator behavior. Retire/replace equipment and verify that retained discovery/state does not leave obsolete entities. Verify blanking, panel power, and Player shutdown separately with the chosen power dependencies. Test bedtime darkness independently of its optional animation and retain the selected wake-up policy across recovery.

## Proposed engineering budgets

These are candidate experiment budgets, not implemented limits, accepted guarantees, or measured results. Select applicable targets and test conditions through the design decisions before reporting a pass. Other capacities and outage durations remain unspecified.

| Measurement | Proposed starting point |
|---|---|
| Rolling file preparation | Approximately five minutes; independent of retention and outage duration |
| Fleet exercise | 32 Players / 64 Frames; not a fixed product limit |
| Connected desired-state observation | Within 1 second p95 on the LAN |
| Calibration input to visible preview | Under 250 ms p95 on the LAN |
| Coordinated visible onset/transition skew | At most 100 ms p95 |
| Tight synchronization qualification | At most 16.7 ms agent-reported skew and 33 ms visible skew p95 when targeting 60 Hz; no genlock claim |

## Recording results

Record scenario/slice, build, equipment/software/media, chosen configuration/policies/budgets, workload/procedure, raw observations, failures, and conclusion. State threshold decision status and evidence type: simulated, integration, or physical. Report distributions and sample counts, not best runs.

Capture boot ticket/ID, equipment observation ID, Player/session epoch, release and software/configuration revisions, Outputs/modes, Runs/visible targets/media, cache/readiness, `/v1/player/time` RTT/offset/delay/drift/uncertainty/rejection diagnostics, intended/observed presentation, resource use, and faults. Detect a live process that stops presenting new content. Results qualify only the tested envelope; record exclusions and rerun affected checks when it changes.
