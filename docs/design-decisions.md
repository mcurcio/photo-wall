# Design decisions

**Status: open decisions for the proposed implementation.** Product behavior is defined in [requirements](requirements.md); [architecture](architecture.md) describes the recommended technical direction. This register contains choices that still need a recorded outcome. Resolve the choices needed by each [implementation slice](implementation-plan.md) while independent work continues.

Accepted implementation decisions: [0001 — time/recovery/module contracts](decisions/0001-mvp-time-recovery-and-module-contracts.md), [0002 — registry/enrollment/calibration](decisions/0002-registry-and-enrollment.md), [0003 — coordination and execution](decisions/0003-coordination-and-player-execution.md), [0004 — media publication and recovery](decisions/0004-media-publication-and-worker-recovery.md), and [0005 — native platform and registration fallback](decisions/0005-native-platform-and-registration-fallback.md). These resolve their stated policies; the register below retains remaining qualification and scope questions.

## Initial implementation

| ID | Decision | Starting point and evidence needed | Resolve before |
|---|---|---|---|
| D01 | Delivery scope and operating envelope | Select initial scenarios, expected fleet size, panel modes, network conditions, and equipment. Use the [validation plan](validation.md) to define release criteria; its numerical budgets are candidates. | Initial bench/slice scope before S0 ends; release thresholds before completion claims. |
| D02 | Execution protocol | Define the [execution contract](execution-contract.md): plan validity, epochs/revisions, acknowledgment, idempotency, preparation, commitment, deadlines, supersession, and late/missing participants. Demonstrate that future projection cannot affect current state or devices. | S2 message/schema implementation. |
| D03 | Durable execution and recovery | Specify persisted activation/Run state, logical-time basis, secured assignments, binding generations, and transaction boundaries. Define which projections can be rebuilt. Demonstrate no duplicate activation, rerolled secured content, or stale authority after restart/replacement. | S2 persistence; S3 replacement; S6 recovery acceptance. |
| D04 | Referenced definitions and participants | Define group membership snapshots, typed Frame/Actuator participants, source/Scene revision resolution, and calibration references. A later child launch is a new Run; choose which revision it resolves. Freezing every descendant at parent admission is a possible policy. Distinguish a parent's target union from its current contribution. | Freezing configuration and plan schemas. |
| D05 | Activation, composition, and lifecycle policy | Define priority/protection/stacking precedence, ties, partial overlap, manual/Program ownership, duplicate matching, queue limits/expiry, child timing/inheritance, effect scopes, and cancellation/outros. Specify how ongoing children finish current activity and how an outgoing background completes while its successor progresses. Decide no-owner state and whether persistent schedule overrides need a resource. | S2's scoped lifecycle model; S5 composition integration. |
| D06 | Media eligibility and source scope | Select supported Immich versions, user/library permissions, and an initial query subset for the [central media integration](requirements.md#central-media-boundary). Specify orientation and physical-size/source-quality rules beyond the required exclusions, authored alternatives, and still/motion relationships. Test empty results separately from permission, API, or transport failures. | S4 real media planning. |
| D07 | Acquisition, caching, and outages | Choose whether the central system generates derivatives or acquires only upstream variants; define its gateway, refresh/lead times, cache size, pinning, and retention after media leaves a query within the [central media boundary](requirements.md#central-media-boundary). Define post-lookahead behavior, offline schedule/overlay expiry, and current-state rejoin. Size storage and fan-out bandwidth against an explicit outage duration and conversion workload. | S4 acquisition; S6 resilience claims. |
| D08 | Player rendering and capacity | Qualify pinned Python/PyGObject/GStreamer/GTK/kernel versions, event-loop integration, decoder/GPU sharing, output routing, calibration, and transitions. Select codec/profile and concurrent workloads using two-output measurements, including overlays and reveal preparation. | S3's appliance foundation; media/performance guarantees. |
| D09 | Time mapping and synchronization | Map logical Run time, scheduled time, local monotonic execution, and media position. Define clock-health limits, time-discontinuity behavior, and independent/coordinated/tight timing scope. Start with disciplined system clocks; consider more complex synchronization only if measurements identify clock error as the constraint. | Coordinated playback qualification and supported timing claims. |
| D10 | Fleet provisioning and trust | Implement the required [automatic Player provisioning](requirements.md#player-provisioning): select the PXE image artifact, centrally prepared discovery/trust mechanism, automatic registration and credential establishment, local versus network runtime/storage, identity/cache persistence, rotation/revocation, and binding claim/retirement. Define supported network-boot-capable hardware and choose update integrity, rollout, and rollback mechanics. A common image must not embed a permanent shared fleet credential. | S3 automatic appearance/provisioning/replacement; S6 rollout. |
| D11 | Central tooling and packaging | Choose language/framework, dependency/build tooling, database migrations, launch configuration, and durable storage. The modular central application and separate media worker are the starting topology. Define the minimum operator UI and configuration workflows. | S2 reproducible central launch; complete operator walkthrough by S6. |
| D12 | Color, audio, calibration, and power | Evaluate a silent wall and one SDR working space as initial simplifications. Select initial geometry transforms, photometric controls, preview/commit/revert and live-adoption rules, and revalidation after equipment changes. Define persistent bedtime darkness/wake-up, panel power, Player shutdown, and Power Domain dependencies separately. | Corresponding presentation/equipment features. |
| D13 | History, environmental adaptation, and integrations | Define visible-history accounting, including partial coverage, repetition and motion limits; reproducible selection if needed; ambient sensor cadence/response; Home Assistant exposure; and HA-owned versus direct devices. Specify physical cue versus persistent-state handback and multi-Installation scope. Prefer a small logical capability surface to vendor commands in Scenes. | Implementing the selected history, Sensor/Actuator, or broader topology features. |

For D12, define each Surface's origin, axes, orientation, and canonical physical units before freezing topology/calibration schemas. Millimeters are the proposed physical unit; distinguish physical coordinates from output pixels. For D13, include stable logical entity IDs, discovery/state reannouncement after Home Assistant reconnect, and cleanup of retained state when equipment is retired or replaced.

An experiment may use an explicit temporary assumption. Record it with the result; it does not become a supported production contract automatically. Physical measurements apply to the tested build, equipment, media, and network conditions.

## Later scope

The following are candidates, with no assigned release or delivery commitment:

- Richer Immich queries, semantic curation, related-media selection, and authored still/motion relationships. Keep wall history and exclusions local initially; any future Immich writeback needs an explicit integration decision.
- Full virtual-wall composition, cross-Frame travel, panoramas, advanced effects, mesh calibration, and richer timeline authoring. A panorama does not introduce an implicit exception to the photo-orientation requirement.
- HDR/wide-gamut output, refined cross-panel color calibration, synchronized audio, and more elaborate lighting cues after the relevant output paths are qualified.
- Broader multi-Installation coordination, automatic equipment assignment, and content-aware layouts after the initial operational model works.

Continuous viewer tracking is outside current scope. Actual genlock would require a separate hardware investigation. Generating moving-portrait assets is separate from executing them and does not belong in the runtime architecture.

## Open-source release preparation

Before the first code release, select and add the repository license, inventory dependency licenses—including selected GStreamer plugins and their transitive codec/native libraries—and establish the project's security-reporting route. These are repository-owner decisions; this documentation does not select a license or invent a reporting contact. Verified installation instructions and a declared support/qualification envelope belong with the release.

## Decision records

Record a consequential choice in a short Markdown file under `docs/decisions/` when the first record is needed. Use a stable numbered filename and include:

- Title, status (`proposed`, `accepted`, or `superseded`), date, and decision owner.
- Relevant D-number, affected requirement/contract, and the concrete problem.
- Chosen behavior, alternatives considered, tradeoffs, and consequences.
- Evidence: tests, measurements, versioned primary documentation, or an explicit rationale.
- Links to affected implementation, documentation, and any superseding decision.

Update this register and the affected reference pages in the same change. A decision that changes product behavior must explicitly update [requirements](requirements.md). Keep performance candidates in [validation](validation.md) until an accepted decision adopts a threshold and qualification supplies the evidence.
