# 0017 — The Node redesign (r3): requirements and current choices

**Date:** 2026-10-06 · **Layer:** system design (what binds the Node programme, and the shape it builds toward) · **Status:** This is a short record of the Node redesign, revision 3, as the owner answered it on 2026-10-05 and 2026-10-06. It is checked against [0016](0016-central-and-node-relationship.md), which ranks first. The requirements below bind. Every current choice below can be revised, and each one names the owner answer it comes from. The full design (r3) is working state outside the repository; this page is the version kept in the repository.

## Why this exists

[0016](0016-central-and-node-relationship.md) reopened [0015](0015-player-base-layer-and-health-overlay.md) wherever 0015 assumes Central is in charge. Revision 3 of the Node redesign is the result. It replaces 0015's Authority context, Central link, permits and cursor feeds with a Node that runs itself and reports through one Node API. Where 0015, the [Player architecture](../player-architecture.md) or the r8 design pages (`docs/design/player-health/`) conflict with this page, this page wins.

## Requirements (bind the programme)

These sit beside 0016's six rules, and they do not repeat them.

| # | Requirement | Source |
|---|---|---|
| R2′ | "Cannot reach Central" is a console fact, never a wall fault. It replaces 0015 R2's "the node cannot reach Central" overlay. | Owner, redesign interrogation |
| R6 | A 4 GB Raspberry Pi 5 is supported. | Owner, 2026-10-03 |
| R12 | **Frame-centric overlays.** A fault is shown on the Frame it concerns, not across its whole Display. A Display-scope fault covers that Display's Frames, and a Node-scope fault covers all of the Node's Frames. This replaces 0015 R10's full-screen overlay. The overlay itself stays semi-transparent, with content visible beneath it. | Owner, artifact comment 2026-10-05 |
| R13 | **Pipes stay apart.** The fleet and host pipe stays distinct from the Show and app pipe. Neither pipe carries or checks a token from the other. | Owner, channel-model gate |
| R14 | No security feature that can fail stands between a Node and its work on the home network. | Owner; 0016 rule 4 |

0015's other requirements (R1, R3–R5, R8, R10's semi-transparency, R11) still stand.

## Current choices (revisable)

| # | Current choice | Owner answer it comes from |
|---|---|---|
| C1 | **The Player never alters the show.** It plays what Central asked for. When it cannot, it says so: an unplayable item, or a list that has run out, is a fault on that Frame, reported and overlaid. It never skips, loops, substitutes or reorders. A crash guard stays, so content cannot crash-loop the Node, but the guard reports a failure to play and never edits the show. | IQ1, 2026-10-05 |
| C2 | **Central's Planner sends a list per Frame.** A list that runs out is a fault, not a loop. The Planner may author "repeat until E"; the Node never repeats on its own. | Gate Q3; r2 gate OQ3 |
| C3 | **The Node API is a NATS leaf.** Each Node runs a NATS server (the Node bus) on loopback. Its leaf link goes to a hub that ships inside Central's deployment unit. The hub has one account per Node, with no imports and no exports. Intent comes down as a read-only mirror. Facts and state go up through a per-Node uplink queue, which Central stores and commits before it acknowledges. Bulk bytes stay on HTTP. | Gate r3 OQ1 ("nats-leaf"); chat 2026-10-06 |
| C4 | **The Node API is a plugin model.** Each component writes only its own namespace, described in `contracts`. Central is one client of the API, and a future local web server would be another. The API never judges and never gates. | Artifact comment 2026-10-05; gate Q2 note |
| C5 | **The NATS rules are held by structure, not convention.** Only `nodeapi` imports the NATS client: an import contract enforces this, and it landed in E1. Every connect re-declares its streams and buckets. Nothing on a main path waits for a publish acknowledgement. Every fact carries its boot id, component and sequence number. The bus is fenced at 64 MiB of memory. | Gate r3 OQ1 and the measured spike |
| C6 | **Three design rules.** (1) Content is held, not leased. Last good is what the consumer accepted, and anything shown, restarted or recovered is read from files. (2) Each component writes only its own namespace. Each kind has one writer. Central stores a fact before judging it. (3) Nothing on the show path waits: not on Central, the bus, Content, a publish acknowledgement, or another component's decision at runtime. | Design rules of r3, checked against 0016 |
| C7 | **Kinds of time stay apart.** Each Node's wall clock follows a shared NTP reference through chrony. Show time is a position on the Node's monotonic clock and uses FPP MultiSync's correction curve and freewheel. Clocks are never compared across systems. | Gate Q4 note; r2 gate OQ2 ("ntp-signage") |
| C8 | **Memory: optimise now, with no measurement phase.** The readings from the test Pi show the programs use about 400 MiB. The 2509 MiB preparation peak comes from release roots unpacked as tar. The cuts: the app and App Manager roots become squashfs images, mounted in place (app 1180 → 288 MiB, manager 261 → 62 MiB). Each image is held once, with no unpacked or import copy. Each cap is set from its reading: 2 × the peak, at least the peak + 32 MiB. The Player's cap never falls below its own configured budgets. Double counts are removed. One device-class line table, held by a test, sets every cap. This leaves a content line of about 0.85 GiB on a 4 GB Node (about 1.04 GiB once AppManager is gone). A cap that is too low shows itself through the existing `oom_kill` facts. The plan supersedes the 7 GiB-era budget in the [4 GB memory design](../node-4gb-memory-design.md). | Gate r3 RQ1 note; chat 2026-10-06 |
| C9 | **Tests.** Integration tests that exercise functional requirements are preferred over unit tests of internal code. A test may be deleted when moving or maintaining it costs too much, but never when it is the only proof of a functional requirement, and each deletion is listed. A later epic replaces PR 46's per-module unit tests with a few functional integration tests. | E1 discussion, 2026-10-06 |
| C10 | **The programme, refactor first.** E1 splits the Node codebase so that each context lives in one directory (`appliance/kernel`, `host`, `boot`, `apps`, with `display_host` and `health`). One layers contract holds the split. Retiring code is not moved. The design's recommended order after E1: E2 memory cuts and E3 Node API foundation in parallel; then E4, the tracer (node-pid1 leg `offline`); then E5 Central intent and recording, E6 Content and the local app lifecycle, E7 Frames, faults and overlays, and E8 the Player executor and time, in four parallel lanes; and last E9, removal and qualification. The owner has answered "pursue E1 first". The order after E1 is the design's recommendation. | Gate Q5 note; E1 discussion |

## What this replaces

| Earlier text | Replaced by |
|---|---|
| 0015 "Shape": the Authority context, the Central link and its three sessions, facts read by cursor from each publisher's feed | C3–C6: the Node API. Health reads every namespace's conditions, and the overlay instruction stays a direct edge from Health to Display. |
| 0015 "Central link and permits": permits with lifetimes, waiting for a fresh permit after a restart, the stage-document pass-through | C6 rule 1: held documents never expire, and a restart reads files. Admission is local (epic E7). |
| 0015 "Health overlay mechanics": the full-screen tint | R12: regions per Frame, with the full-Output tint as the fallback when there is no Frame. |
| 0015 "Faults, conditions, events": the Central window, permits in the fault definition, the feed ring | R2′, and conditions with Frame, Display or Node scope on the bus |
| 0015 "Design rules" 1 and 3 (Central enters only through Authority; authority arrives with lifetimes) | C6 |
| 0015 "Next gates" | C10 |
| The r8 module and system designs (`docs/design/player-health/`) | This page, wherever they assume Central is in charge |

## Costs

- **Central gains a second transport to the Node.** Nodes now need a NATS hub as well as HTTP. Booting needs both, and they ship as one unit.
- **Central can no longer stop a Node it cannot reach.** The Node plays a deleted or superseded show until it hears from Central again or its list ends.
- **Faults the old design hid are now visible.** Under C1, an exhausted list or an undecodable item tints its Frame, where the old code looped or skipped.
- **There is upstream code on every Node.** nats-server is pinned by version, and its known sharp edges are contained in `nodeapi` and in the 64 MiB fence.
- **Three NATS mechanisms are still unprobed:** the Node's mirror of the hub bucket, an uplink queue that stalls rather than drops, and direction filters that leave mirror and source traffic alone. Tracer E4 proves them first. Each has a fallback that keeps this page's rules.
- **Memory caps rest on one boot's readings.** A wrong cap is found by an `oom_kill` fact, not ahead of time.
- **The epics are large:** about 33–51 hours serial, or 20–30 hours with the lanes.

## History

2026-10-05: the redesign starts from 0016. The owner's artifact comments set C1, R12 and C4. The gate notes set the Q2 plugin model, C2, the Q4 time note and C10's refactor-first. 2026-10-05, revision-2 gate: ntp-signage and authored repetition. 2026-10-06, revision-3 gate: nats-leaf; memory optimised without a measurement phase. The E1 discussion set the big-bang move, tests moving with their code, and C9. PR 44 ([0015](0015-player-base-layer-and-health-overlay.md), [0016](0016-central-and-node-relationship.md), [Player architecture](../player-architecture.md)) predates this page by a couple of days. The owner asked that where they conflict, this page wins. E1 recorded this page.
