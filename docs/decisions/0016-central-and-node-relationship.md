# 0016 — Central and the Node: who helps, who decides

**Date:** 2026-10-05 · **Layer:** declaration (the relationship every Node and Central design is checked against) · **Status:** Owner-written and owner-accepted on 2026-10-05. It sits above the system designs: where [0015](0015-player-base-layer-and-health-overlay.md) or any later design disagrees with it, the design changes.

## Why this exists

A Node CI scenario failed intermittently on 2026-10-05: the Player's identity proof was refused because it carried a Show token that Central re-issues on every read. Investigating it, the owner concluded the Node model "is fundamentally broken ... too heavy in security and generally assumes that Control is always in-charge, which tends to break the reliability of the node's operation." An inventory of the code found 48 places where a Node waits on Central and 43 security mechanisms (about 7,000 lines). Five of them can stop a healthy-looking wall:
- One Player crash lasts the whole boot.
- Content is leased from Central for about 5 minutes.
- Commits need the Node and Central clocks to agree within 0.1 s.
- A cue is all-or-nothing across Players.
- Every boot re-fetches about 2 GB under hard windows.

## Terms

| Term | Meaning |
|---|---|
| **Node** | The hardware, its OS and the host software: a Raspberry Pi 5 netbooted from Central, with no disk. |
| **Player** | The app software that runs on a Node and plays the show. |
| **Output** | A video connector on a Node. A Pi 5 has two HDMI Outputs. |
| **Display** | The physical screen attached to an Output. It is calibrated at the hardware level: brightness, CEC power and input. |
| **Frame** | A virtual area mapped onto exactly one Display, and the thing a show targets. A Display carries one or more Frames. A Frame never spans Displays. A Frame is calibrated for its assignment to a Display: relative position, software brightness, contrast. |

These replace the earlier usage where "Player" meant the box and "Panel" meant the screen. Renaming the existing documents and code follows as its own work.

## Central

Central is the management and operator-observability layer. Operators go there to see Nodes, Displays and Player state, and to create, run and monitor shows. Central has a symbiotic relationship with the Nodes: it boots them, gives them their configuration, and synchronizes shows across them. Central states intent and records everything Nodes report.

## Nodes

Nodes are the hardware layer that gives a show a place to run. A Node is self-sufficient wherever it can be.

Its one hard dependency is booting: a diskless netbooted Node needs Central to start, and that is accepted. Once booted, it relies on Central only for:
- new configuration;
- new shows;
- synchronizing with other Nodes.

## The relationship

1. **Help, not permission.** Central's help makes a Node better, but its absence never stops one. A Node never waits for Central to keep showing content, restart its Player, or recover.
2. **Last good wins, until its own data runs out.** Without Central, a Node keeps running its last good configuration and show. It stops only for a reason inside its own data: the show's schedule ends, its pre-cached content runs out, or a similar failure that comes from missing new data. Central going quiet is never by itself a reason to stop.
3. **Intent down, facts up.** Central owns what should happen, and the Node owns the facts about itself. Central records every fact a Node sends, with how it judged it, and never discards or overrides one. A Node never invents intent.
4. **Trusted by default.** On the home network, a Node is who it says it is. No check that can fail stands between a Node and its work. A security mechanism that can stop a Node is a reliability defect.
5. **Sync is a quality, not a precondition.** Better sync looks nicer, but worse sync still plays. Clocks are never compared across systems.
6. **Failure stays local.** One Node's trouble never stops another Node's show, and one Frame's trouble never stops another Frame. A Node shows its own trouble on its own Displays and reports it.

## Accepted alongside

These come from the same day's channel-model gate ([briefing](https://claude.ai/artifact/DqJuEdRVxNR9YHobjGJHUB), private to the owner):
- **Requirement:** Central receives and records every message a Node sends, before and separately from judging it. A refused message is still recorded, with the reason.
- **Current choice (revisable):** Central and a Node talk over five channels split by subject: Observability, Host, App lifecycle, Output, Show. Each has its own rules for loss and timing. Protocol and mechanism questions on those channels wait for the redesign below.

## What follows

- A first-principles redesign of the Node and Central relationship, measured against this declaration and the inventory. It reopens [0015](0015-player-base-layer-and-health-overlay.md) wherever 0015 assumes Central is in charge.
- M1 of the Player health build ([PR 46](https://github.com/mcurcio/photo-wall/pull/46)) stays paused before B12 until that redesign says what M1 should finish with.
