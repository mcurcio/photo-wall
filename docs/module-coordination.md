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
lock. Lock order is coordinator, runtime (when needed), current Player rows,
Output and Frame rows, then media quota/reference lock. Registry's enrollment,
binding, placement, calibration, Identify, profile, deletion and retirement
writes serialize through the coordinator lock; equipment writes also take Runtime's
lock before their row locks. Media grants take the coordinator lock before
authorization and refuse a drained Player. No database transaction spans downloads,
decoder work or WebSocket sends. Configuration reads and offers validate current
Player epoch and Frame generation while holding the same transaction's row locks.

A fleet transaction that also needs a device takes, after Coordination and Runtime, that
device's fence (`central/fleet/device_fence.py`), then the offer quota row, the device insert,
the session, the producer and evidence or commands. The fence is a per-device reader/writer
lock: every node request and operator action on a device holds it shared (`act`), so no request
waits on another's slow COMMIT; a boot offer, a node enrolment and retirement hold it exclusive
(`change`), and a `change` not granted within the lock timeout answers 503 `node_device_busy`
with Retry-After. A transaction holds at most one fence: taking its device again is free, an
upgrade or a second device is refused. A post that writes a producer's evidence also holds that
producer's row (`NodeSessions.authenticate_producer_in`), so one producer's posts run one at a
time and a retry finds the first post's rows. Operator device reads take none and read one READ
ONLY snapshot. The release writer takes its own lock, alone. There is no fleet-wide lock.

A Node's display exchange (`POST /v2/node/display`, `central/fleet/node_display.py`) takes only
that Node's lock (issue #127; `central/transaction_locks.py` `acquire_node_lock`, the two-key
advisory lock `NODE_LOCK_CLASS` and `hashtext(device_id)`), never Coordination or Runtime, so one
Node's exchange never waits on another's. `Coordinator.display_admission_in` and
`display_withdrawal_in` refuse (`node_display_node_lock_required`) unless the caller holds it, and
read the Player row `FOR SHARE`, so a binding write on that one Player (`FOR UPDATE`) orders
against it. Lock order: Coordination, Runtime, then a Node lock (an operator calibration trial
and an exchange reporting a completion take it after them); a holder of a Node lock never takes
Coordination or Runtime after it. The exchange keeps `lock_timeout` 5 s for its own statements.

**A lock that times out is a retryable refusal** (`central/lock_waits.py` `mount_lock_refusal`,
mounted once by `create_app` over the operator and node routes): any request whose transaction
hit `LockNotAvailable` answers 503 `{"error": "lock_busy"}` with `Retry-After: 1`, never a raw
500, after logging the holders of every advisory lock someone waits for, read on a connection of
its own. What an operator does with it: [runbook › Node control](runbook.md#node-control).

A row, and a projected Commit, changes only when its meaning changes. A readiness report writes
the Player's feedback row; it rewrites a grant (`execution_commits`), a group's status or the
Player's configuration only when that changed. A delivery projects one Commit per grant (group,
readiness sequence), so a grant written later never changes a Commit the Player accepted, and an
unchanged state is the same control delivery (`state_digest`), never a new one.

An idle-only equipment drain persists an affected-Output snapshot and cancels
outstanding atomic groups in the same transaction. New offers omit that Player's
Outputs; old plan delivery carries cancellation. A prepared drain may abort only
after authorization expiry plus margin, a fresh current-epoch applied control
receipt, offer/configuration revalidation and a repeated idle check. The aborted
record remains as a tombstone until a new attempt replaces it. `stop_committed`
persists across restart and deadline expiry and cannot use the prepared abort.
This foundation exposes no remote command or stop permit, does not interrupt an
active Run, and has no committed-stop reconciliation or per-Output handoff. Those
require the accepted fleet trust and Runtime policies.

The complete configuration has its own persisted monotonic revision per Player
epoch. Comparing the normalized full snapshot detects removal and changes to
either Output; taking a maximum of Frame revisions would miss independent edits.
All bindings permit calibration; only `enabled_outputs` permit execution. A cold
Player still needs a freshly rotated epoch even when the stored configuration is
unchanged. What a Frame keeps after a layer (`Layer.after_end`) is an explicit central
decision ([execution contract](execution-contract.md#what-a-frame-keeps)); it changes nothing by
default, including temporary overlays.

Short output-identification requests are operational registry state, separate
from Frame and Scene content. Central stores at most one request per Player,
with its Output, current authority epoch and fixed 15-second expiry; a new
request supersedes the prior one. On each delivery, Central includes the cue
only while the Player epoch is current and the Output remains connected and
unbound. Expired or no-longer-eligible requests are removed. The cue appears in
both the REST state and WebSocket state envelope, and its remaining duration is
computed by Central for that response. Acceptance means the request was queued;
it is not a playback observation or proof of visible output.

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
