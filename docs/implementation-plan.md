# Implementation plan

Photo Wall is implementing this sequence; see the [durable checklist](implementation-checklist.md) and [evidence](evidence/README.md) for the actual verified state. Appliance and measured hardware results are still outstanding. The sequence implements the [architecture](architecture.md) and [requirements](requirements.md).

Use the [validation matrix](validation.md#acceptance-matrix) for evidence and [design decisions](design-decisions.md) for open policies, release scope, and performance budgets. All slices follow [recursive development and agent orchestration](../CONTRIBUTING.md#recursive-development-and-agent-orchestration): design modules first, then integrate child implementations and evidence from the bottom up.

## Dependencies

| Slice | Dependency | Result |
|---|---|---|
| S0 — Scope and contracts | Requirements and design decisions | Bounded first scenarios and implementable contracts |
| S1 — Physical Player | S0 bench scope | Qualified rendering path on two Outputs |
| S2 — Central execution | S0 runtime/persistence scope; parallel with S1 | Observable execution through simulated devices |
| S3 — Managed appliance | S1 dual-output viability; S2 configuration/execution boundary | Automatic PXE startup and central registration, followed by Frame binding |
| S4 — Live media | S2 planning/commitment; S3 Player | Live upstream media delivered exclusively through the central show system |
| S5 — Composition and schedules | S2 lifecycle; S4 playback/preparation | Nested overlays and calendar transitions |
| S6 — Recovery and operations | Integrated preceding slices | Reproducible deployment and selected operator workflows |

S1's one-Pi/two-panel result enables S3. Cross-Player timing work can continue alongside S3/S4 but must finish before coordinated playback claims. Include failure injection and diagnostics throughout; S6 consolidates recovery evidence.

## S0 — Scope and contracts

Select initial scenarios, equipment assumptions, media fixtures, and evidence. Resolve decisions that block those scenarios.

Turn the [execution contract](execution-contract.md) into versioned examples covering Runtime intent, side-effect-free projection, planning, acquisition, commitment, execution, and feedback to lifecycle ownership. Specify durable Run/activation/assignment state and binding authority separately from rebuildable projections. Choose initial tooling and a reproducible launch path.

**Outcome:** bounded implementation tasks with inputs, owned state, observable results, and explicit unresolved choices.

## S1 — Physical Player

Prototype the proposed Python/PyGObject Player with embedded GStreamer, native composition, and persistent GTK surfaces on the intended appliance build. Exercise independent stills/videos, calibration, crossfades, and covered-video reveal on both Outputs. Measure aggregate decoder/GPU capacity, seeking, continuity, and failures using the [qualification procedures](validation.md#qualification-procedures).

**Outcome:** a reproducible viable build and workload profile. Adjust media profiles, composition, or equipment allocation when measurements require it; retain Frame identity and central Scene semantics.

## S2 — Central execution with simulated devices

Build a minimal central application with durable configuration and one activation-to-output path. Use controllable time, simulated Players, and a recording Actuator adapter. Include typed Frame/Actuator participants, exact variant identity, plan validity, current-state reveal, and separate acquisition, decoder readiness, and whole-Player capacity.

Reject stale/duplicate instructions; return consequential execution failures to Runtime. Exercise central restart before dependent modules rely on persistence.

**Outcome:** traceable prepared and committed execution, with no early lifecycle changes or cues from projection and no rerolling of secured assignments after restart.

## S3 — Managed, replaceable appliance

Deliver the common image automatically through PXE, followed by central discovery, registration, and visibility before Frame assignment. Require no per-device configuration, SSH, endpoint entry, or manual credential provisioning. Establish identity/authentication automatically through centrally configured provisioning/trust services. Define identity/cache persistence and reimaging.

Add authenticated desired-state reconciliation, Output identification, subsequent central Frame binding, and replacement/retirement authority. Runtime storage choices must preserve the automatic provisioning path.

Provide calibration preview, commit, revert, and version retrieval. Keep persistent aperture geometry distinct from equipment-dependent correction and define when changes affect active output.

**Outcome:** a fresh device appears centrally after PXE startup. An operator then binds/calibrates two Outputs and replaces the Player while preserving Frames and rejecting retired bindings.

## S4 — Live compatible media

Connect a narrow AssetSource query to a declared Immich version and permission model within the central system. Players remain Immich-unaware and receive all assets exclusively through the central show system: no upstream API details, keys, direct calls, or redirect paths in Player configuration or protocol. Verify playback with direct Player-to-Immich network access blocked.

Enforce source and Frame suitability before selection, then qualify the presentation variant. Add bounded acquisition, integrity checks, rolling assignments, and retention without conflating source refresh, lookahead, decoder preparation, and outage storage.

Exercise changing query results, acquisition failure, and deletion before and after commitment. Preserve authored alternatives and fallback behavior rather than relocating spatial roles.

**Outcome:** an ongoing Run receives new eligible media without restarting, while secured assignments retain their exact content and empty pools preserve compatible fallback output.

## S5 — Composition and scheduling

Integrate nested spatially authored Scenes, per-target contributions, and an independent overlay crossing a calendar boundary. Implement the scoped arbitration, repeated-activation, outgoing-background, and child-reference policies. Include lamp-covered and lamp-uncovered cases, current-position reveal, natural completion, downward cancellation, and Program expiry.

**Outcome:** recorded output and traces demonstrate the lifecycle and composition scenarios in the validation matrix, including no replay of hidden cues and no premature output from future children.

## S6 — Recovery and operator workflows

Exercise central/Player restarts, outages beyond lookahead, cache pressure, current-state rejoin, and stale equipment authority. Package central launch, migrations, configuration, appliance provisioning, diagnosis, and the operator path from enrollment to scheduled playback.

Implement the selected update/rollback mechanism and integration controls. If included in release scope, qualify Home Assistant controls, environmental adaptation, power handling, and bedtime/wake behavior; optional decoration must not block the required bedtime outcome.

**Outcome:** repeatable deployment/recovery evidence for the chosen scope, identifying unimplemented capabilities and unqualified environments.
