# Central execution coordination

Status: accepted central implementation contract, revised for transaction-bound domain interfaces and typed outcome delivery.
Extends [decision 0003](decisions/0003-coordination-and-player-execution.md).
The central application owns this module, its PostgreSQL schema, and integration. Players hold only current session state.

`Coordinator` depends on the public `CoordinationMedia` port for authored candidates, offer pinning, grant acquisition, and expiry. It does not call media repository private methods or read/write media tables. The media implementation owns its SQL and locks. Installation/registry, Runtime, Planner, coordination, and media operations enter through named application operations with caller-bounded transactions; one domain does not reach into another domain's private storage.

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
allowed per Player epoch; missing acknowledgment produces backpressure and degraded
health. Horizon renewal is quantized to 30 seconds while retaining at least the
configured 300-second preparation horizon. A manifest change may produce an
earlier revision. Neither an acknowledgment timeout nor changed live membership
permits rerolling possibly secured bytes: all unexpired offers and confirmed locks
are conservative Planner locks under their current binding authority, including
those recorded before a same-key Player epoch rotation. A matching enabled
Frame/Output/generation keeps that exact content identity. Historical offers
only constrain content: fresh plan revisions, offer budgets, readiness and
commitments remain current-epoch scoped. Retired Players, disabled Outputs,
stale bindings and expired content do not regain authority. Conflicting live
content records fail closed with `inconsistent_content_lock`. Old offers in
the current epoch can accept delayed byte-security feedback but never authorize playback of an
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

Each observation is also normalized as a typed `ExecutionOutcome` carrying kind,
time, Player/Plan/assignment identity, result, and bounded detail. An
`ExecutionOutcomeRouter` delivers the same value to Runtime and Planner before the
audit event is accepted. Runtime preserves logical lifecycle semantics when a
physical execution fails; Planner requests replanning for preparation/execution
failure without silently changing an already secured assignment. Success and
ordinary observations preserve the existing plan. This handoff replaces private
cross-module callbacks with an explicit application contract.

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
