# Persistent coordination

Status: accepted implementation contract, 2026-09-05; integration under development.
Extends [decision 0003](decisions/0003-coordination-and-player-execution.md).
The orchestrator owns this module, all PostgreSQL schema and integration.

`RuntimeStore` restores the pure Runtime from a single PostgreSQL snapshot under a
transaction advisory lock, applies one command/current-time advance, then persists
the complete snapshot and revision. Admission IDs, snapshotted Scene trees and
Program boundaries therefore survive process restart. No in-memory Runtime is an
independent authority. Future projection runs on a copy and does not persist future
activation or Actuator effects. Commands validate before committing; a failure
rolls back the whole change. Schema migration and runtime locks use distinct keys.

`Coordinator` serializes plan/readiness/commit changes under a second transaction
lock. Lock order is coordinator, runtime (when needed), all current Player rows in
ID order, Frame rows, then media quota/reference lock. Registry operations never
take a coordinator lock. No database transaction spans downloads, decoder work or
WebSocket sends. Configuration reads and offers validate current Player epoch and
Frame generation while holding the same transaction's row locks.

The complete configuration has its own persisted monotonic revision per Player
epoch. Comparing the normalized full snapshot detects removal and changes to
either Output; taking a maximum of Frame revisions would miss independent edits.
All bindings permit calibration; only `enabled_outputs` permit execution. A cold
Player still needs a freshly rotated epoch even when the stored configuration is
unchanged. `Layer.retain_on_expiry` is an explicit central decision limited to opaque
stills. It is false by default, including temporary overlays.

Offers use a stable per-epoch plan ID and increasing revisions, retaining exact
manifests until their execution lease expires. At most 64 outstanding offers are
allowed per Player; missing acknowledgment produces backpressure and degraded
health. Horizon renewal is quantized to 30 seconds while retaining at least the
configured 300-second preparation horizon. A manifest change may produce an
earlier revision. Neither an acknowledgment timeout nor changed live membership
permits rerolling possibly secured bytes: all unexpired offers and confirmed locks
are conservative Planner locks under their current binding authority. Old offers
can accept delayed byte-security feedback but never authorize playback of an
obsolete revision. An explicit release acknowledgment may later optimize this
conservative expiry policy; it is not assumed from silence.

The coordinator builds required groups from Runtime intents, including intents
whose media/Frame is unavailable. All Frame contributions sharing a root and exact
cycle start are required in that cue; omitted targets are absent. A group identity
also includes its binding/Player epoch cohort so replacement/rejoin is explicit.
Normal due work commits only within the 5-second preparation lead, with fresh
(initially 2-second) whole-Player capacity/clock/prepared feedback from every
required assignment. First reconciliation of an already-active cue receives a
5-second late-join preparation deadline and retains its original media origin.
Failure at a normal intended start skips that cue. Recording Actuator effects
remain current-state operations owned by Runtime and are never projected effects.

Readiness sequence numbers increase across an epoch. A secured receipt checks the
retained offered manifest and creates an immutable byte lock before commitment.
Failures invalidate prepared state and commitments, persist a bounded coded
lifecycle event, and feed acquisition cooldown/replanning. Already executed output
cannot be retroactively revoked; Players independently reject invalid readiness
and use compatible fallback. Observations are evidence, not execution authority.

Media references are inserted in the offer transaction before it becomes visible.
The gateway rechecks epoch, binding and unexpired exact grants for every request,
then acquires a transfer reference before opening a canonical nonsymlink file.
Ready blobs cannot be offered while deleting or corrupt. The media repository's
quota/ref lock is shared by offers, publication and GC; no source URL enters this
module's Player payloads. Filesystem publication remains a separate journaled
operation in [the media module](module-media.md).

Required checks: real PostgreSQL concurrent duplicate admission; restart preserves
Run identity; full configuration revisions; atomic offers/content locks; late and
reordered readiness; epoch/rebinding races; complete required-group readiness;
deadline skip and current-position rejoin; lease-safe old offer protection and
bounded backpressure. These checks do not establish hardware timing or rendering.
