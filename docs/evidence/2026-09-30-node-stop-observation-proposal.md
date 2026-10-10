# Owned stop operation proposal — 2026-09-30

Status: **design contract; automated checks are in the test suite. Not qualified.** This scopes the user's direction that stopping exposes an owned Promise/Future whose successful completion means the stop postcondition is satisfied. Transient platform observations belong inside that operation. It does not change command admission, D16, D17 or physical qualification.

**Revised 2026-10-01.** Stop permits, executor sealing, no-effect revalidation and Central drain discharge were removed (see the [node domain model](../player-node-domain-model.md#debian-closure-and-app-effects)). The owned stop operation, local intent journal, `_old` check and recovery obligation below are unchanged. The authorization table and `StopRequest` shape now describe that contract; the dated defect and implementation records remain history.

The [node domain model](../player-node-domain-model.md#debian-closure-and-app-effects) owns authorization and immutable repair. The [red/blue refinement](../player-fleet-red-blue-refinement.md) owns retirement, durable drains and expiry as a restriction on new admission. This proposal supplies a Linux adapter contract beneath those policies. Source references below describe the reviewed dirty readiness checkout, not a released commit.

## Observed defect and responsibility

The actual PID1 run `pid1-success-5cbf8bb9` (runner 99225) recorded old Player PID 414 stopping at 23:54:34–35, but the broker retained only `intent_stop` and `effect_unknown/stop_outcome_unknown`. It was interrupted after 434.48 seconds; the owned container was removed. The exact originating exception was not preserved, so a particular procfs error is a hypothesis, not the established cause. Linux fixture diagnostics are being extended to reproduce and preserve that cause without changing production behavior.

[SystemdAppProcessDriver.stop](../../appliance/apps/process_linux.py) currently submits `systemctl --no-block stop`, then samples for 15 seconds. Its stop observer reuses checks designed to prove a running process, including live root/cgroup metadata. Those facts can disappear during ordinary teardown. [OnlineEffectBroker.execute](../../appliance/apps/online_broker.py) collapses a false return and every exception into the same unknown outcome. Its recovery path does not later complete an ambiguous stop from exact quiescence. Thus a later systemd dead state and the earlier unknown are compatible observations; the adapter failed to preserve a continuing operation, and its caller lost the explanation.

The architecture correction is an **owned stop operation**, not more identical polling samples. PID1, procfs and cgroupfs do not expose one atomic multi-file snapshot. Safety must derive from exact identity, exclusive ownership, no automatic replacement and a monotonic quiescence postcondition. Two matching snapshots alone do not establish that argument.

## Interface and execution ownership

The process-driver port returns `StopOperation` from `stop(StopRequest)`. The request binds the existing operation ID, boot, exact old `RunningApp`, immutable command identity and a node-local final stop-dispatch deadline (the recovery stop deadline). Admission remains the broker's responsibility; the adapter enforces that supplied deadline immediately before a new effect dispatch.

`StopOperation` provides `done()` and `result()` and may provide an awaitable view. A successful result is a typed `StopCompleted` witness. An unresolved operation remains pending. An exceptional completion means a named contradiction or irrecoverable loss of the ability to establish the guarantee, with sanitized diagnostic context. It must not mean that one read failed or that a caller waited too long. A caller's timeout, cancellation or dropped handle does not cancel the underlying operation, remove its journal, release ownership, resend stop, or permit replacement. An async view must shield the owned operation from waiter cancellation.

The Linux adapter owns submission, observations, retry scheduling, durable operation state and recovery. The broker never interprets `ActiveState`, missing procfs files, cgroup samples or transient subprocess failures. It consumes successful completion once, journals the existing `stopped` event, and invokes the existing immutable target/fallback continuation. Failure retains the drain and roots and reports the typed reason; it grants no fallback or second stop.

Use the existing single-writer execution model: `driver.service(budget)` advances pending subprocess I/O and stop observations on the serial broker loop. `Popen`/nonblocking result collection replaces a blocking stop-command wait; bounded observations belong to the adapter. There is no background thread writing `BootStore`, no second executor and no lifecycle-owned polling state machine. A future-like port is compatible with this cooperative scheduler; a whole-program asyncio conversion is unnecessary.

The adapter writes a dedicated versioned stop-operation record using the existing [BootStore](../../appliance/kernel/boot_store.py) atomic/fsync and poison-on-write-failure behavior. It records immutable request identity and captured process/cgroup evidence before dispatch, submission progress and terminal completion. The lifecycle journal remains the sole owner of protocol event order and target/fallback budgets. The records link by operation ID and exact request digest; neither recreates the other's policy. Recovering the same request reattaches; a conflicting request refuses. Completion must survive a broker crash before the lifecycle consumes it.

[broker_runner](../../appliance/apps/broker_runner.py) must service local operations outside the session-grant/network-success gate. Observation is not new remote authority. HTTP retries remain bounded and must not starve local servicing or app-proof handling; measure that integration explicitly. A watchdog can report pending duration and last observation error without completing the future as failed. Unexpected programming errors are reported distinctly with a local traceback and a stable fault code; they are not swallowed as ordinary polling failure. Store poisoning prevents further effects and terminates the owner safely.

## Stop postcondition and identity

Before admitting the operation, capture the exact boot, PID/birth ticks, app epoch, invocation ID, unit name, root identity and cgroup path plus filesystem identity. Hold an open cgroup directory descriptor while the owner lives and compare path identity to that captured object. The fixed unit name is safe only while the existing exclusive owner prevents another launch, the service has `Restart=no`, and the app cannot delegate or populate a replacement cgroup. Losing those premises is a contradiction, not a successful stop.

Completion requires the exact old process to be gone and its owned subtree to contain no live processes, with no conflicting invocation or pending owned start/restart. It does not require live-process root metadata to remain readable after teardown. A missing `/proc` read alone is insufficient: the current helper conflates missing, unreadable and malformed data, so the adapter needs a typed absence observation. A live old PID with disappearing root metadata is pending; a reused PID must never be signaled or mistaken for the old process. A different invocation or replaced cgroup is a named contradiction.

Linux cgroup v2's `cgroup.events` `populated` value covers live processes in the subtree, not just the main PID. A completed systemd job is useful evidence but does not replace the subtree requirement. A disappeared original cgroup can support completion only with the captured identity, exact process absence and exclusive-owner/no-replacement premises; permission/read errors cannot impersonate disappearance. Reattachment after owner restart must validate boot and persisted identity; a same-path different cgroup is not the old subtree. [Kernel cgroup v2 documentation](https://cdn.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)

An optional pidfd gives the live owner a stable process handle and readiness on process termination. It does not prove subtree emptiness and cannot survive an owner restart by persisting an integer descriptor. Python and kernel support can be tested without a new package, so assess it during implementation as a small adapter improvement, not a prerequisite for changing domain policy. [pidfd_open documentation](https://www.man7.org/linux/man-pages/man2/pidfd_open.2.html)

Systemd v257 exposes StopUnit's job identity and JobRemoved notifications; subscription before submission avoids losing a fast completion notification. The built base has busctl/libsystemd but no established Python D-Bus binding. A new sd-bus binding or helper would require packaging and ABI work. Start with the existing systemctl transport if its bounded nonblocking subprocess can preserve the contract; do not invent a fragile text-monitor notification protocol. A job API is an alternative implementation of this same port, not authority to declare the entire subtree empty. [Systemd v257 manager interface](https://github.com/systemd/systemd/blob/v257/man/org.freedesktop.systemd1.xml)

## Authorization and crash contract

| Boundary | Required behavior |
|---|---|
| Before durable stop intent | The broker verifies both immutable roots and the exact old process for the latest accepted stage. No Central permit or deadline participates; Central reachability is not required. |
| Durable intent before submission | Persist adapter identity before any effect. A restart reattaches and observes. It never treats intent as proof that a syscall happened. No blind redispatch after a crash. |
| Submission response lost | Submission status may remain unknown while the future observes the postcondition. A transient transport error is not proof of either no effect or failed stop. |
| Old process later exits naturally | Durable authorized intent plus exact old-process absence, original subtree quiescence and exclusive ownership may satisfy the requested postcondition. Report observed quiescence, not a claim that the stop signal caused exit. A submission receipt is diagnostic evidence, not an additional authority prerequisite. |
| Old process remains alive after the dispatch deadline | Do not dispatch again. Retain the operation and roots and report `effect_unknown`; the HostCore recovery obligation owns escalation. |
| Completion after session loss | Observation completes locally and the switch continues to target/fallback. Events queue and are reported on reconnect; Central event intake is evidence only, subject to its authenticated carrier rules. |
| Retirement or newer desired stage | A newer stage replaces an accepted one only before its stop intent or after it completed. A stop or start in flight finishes the frozen attempt/fallback first. Remote revocation cannot recall a delivered physical effect; do not claim instantaneous revocation during partition. |
| Completion persisted before stopped event | Replay the same completion into exactly one ordered event. Crash/retry cannot allocate a second stop, new operation or app epoch. |
| Stopped event before target/fallback spawn | Preserve existing target-intent and fallback-intent budgets. A stop future cannot reset them. Recover the exact launch or use the already allowed single fallback; do not retry an ambiguous fallback spawn. |
| Owner reboot or contradictory identity | Volatile same-boot operation cannot be reattached across boots. Preserve Central uncertainty and existing recovery policy. Never reinterpret new-boot absence as old-operation completion. |

The durable intent/submission crash gap is not inherently a permanent failure: the contract requests a final state, not causal attribution. Conversely, absence without a valid admitted operation is not permission to activate a new target. Both halves are necessary. Local record migration must handle older records that lack captured cgroup identity conservatively; it cannot synthesize evidence from a later unrelated unit.

## Required acceptance before implementation is considered complete

Deterministic adapter tests must cover ordinary active→deactivating→terminal teardown, delayed job completion, main PID exit with a surviving descendant, zombie/proc-root disappearance, permission/malformed reads, repeated transient failures followed by recovery, cgroup disappearance, same-path cgroup replacement, PID reuse, conflicting invocation and pending restart. Assert the future stays pending for transient observations and completes only with the entire postcondition. Test waiter timeout/cancellation without operation cancellation and bounded scheduler progress while HTTP fails.

Crash tests must interrupt before intent, after intent/before submission, after submission/before receipt, after completion/before persistence, after persistence/before event, after event/before spawn, and during each target/fallback intent. Restart repeatedly at each boundary. Assert no repeat dispatch, exact immutable permit bytes/deadline, no renewal, no reset of fallback budget, no event duplication and no no-effect seal after intent. Include natural exit on both sides of submission and prove the recorded claim is quiescence rather than signal causation.

Authorization tests must cover command/session expiry before dispatch, permit expiry at the final dispatch check, completion after expiry, revocation/retirement with and without a previously admitted stop, recovery-only permit receipts, pre-existing executor seals, historical records without new evidence, boot changes, journal corruption/write failure and a programming exception. Reuse current domain/DB tests rather than reproducing their admission policy in adapter fixtures.

The actual Linux reproducer must preserve the underlying diagnostic from the current failing production behavior first. Then rerun the real PID1 cold→online success, intentional target-failure/fallback and expired-permit no-effect scenarios using exact rebuilt base/component identities. Require actual final PID/birth/invocation/cgroup agreement with the accepted link, ordered effect events, unchanged no-effect permit deadline, no manual reconcile/ACK, and verified container cleanup. Include deliberate observation delay beyond the former 15-second limit while communications continue, and a broker restart while stop is outstanding. A passing package-installed fixture remains distinct from the complete production squashfs root and physical Pi/HDMI qualification.

## Implementation split and review gate

1. Define the StopOperation port and deterministic model with lifecycle-policy review; choose internal durable schema and legacy-record refusal.
2. Implement the Linux adapter's owned operation and scheduler, with one writer and typed diagnostics; independently review identity/absence proof and crash boundaries.
3. Integrate completion consumption into broker/runner without leaking platform polling or changing seals, event grammar, authority or fallback budgets.
4. Run deterministic and PostgreSQL regression checks, then exact real PID1 reproducer and all three scenario families; rebuild affected base ABI/package/squashfs provenance.

This proposal does not claim those checks passed. Existing full-base artifacts and successful earlier runtime milestones remain historical evidence for their exact sources; implementation of this design will require refreshed artifacts. The pending security proposals, D16 decisions, mixed-version rollout checks and physical qualification remain in the original remaining-work scope.

## User-directed bounded recovery escalation

The user's subsequent direction adds a concrete recovery requirement: if an admitted Player stop/control operation cannot restore operational control within a bounded interval, the base reboots the device to recover. This is authorized design and local implementation/test scope, not permission to reboot physical equipment, deploy or run privileged shared CI. A general Central network outage is not a trigger. Controlled replacement means replacement after proven quiescence or a kernel reboot; it never means starting a second Player beside an unproven old one.

Reuse the independent [HostRunner](../../appliance/host/host_runner.py), base-owned reboot driver and durable intent-before-effect pattern. Add a narrow local recovery supervisor in that base ownership, independent of Player, AppManager and the broker event loop. Its local servicing must precede session/network work and must not require a valid Central carrier to act on an already armed local recovery obligation. This is an explicitly selected local recovery authority, not a fabricated remote operator command and not approval of the separate pending reboot-carrier renewal proposal.

Before dispatching an admitted online stop, the broker registers an immutable recovery obligation over a narrow root-only local port: boot/operation identity, old process, original stop deadline, target/fallback identities and configured recovery policy. HostCore owns the recovery journal and acknowledges persistence before dispatch may proceed. The existing HostCore unit deliberately makes the broker journal inaccessible; preserve that boundary. Do not grant broad journal/root access or transfer the app credential. Define a bounded authenticated local registration/completion message, with peer-credential and immutable-request checks; duplicate registration cannot extend deadlines. HostCore can observe the fixed unit independently. An application heartbeat or manager claim cannot renew the obligation or mask a stuck stop.

The stop-escalation deadline derives from the actual production unit's effective `TimeoutStopUSec`, whole-cgroup kill policy and a measured scheduling/observation margin. The current V2 properties set `KillMode=control-group` but do not explicitly pin TimeoutStopSec. Implementation must capture/validate the effective finite PID1 value or pin it centrally in the shared unit policy; it must not reuse the adapter's arbitrary 15-second wait. Target/fallback restoration has a separate finite budget derived from their existing single-attempt launch/health contracts. A recovered broker inherits the original deadlines; process restart, network retry and repeated crash cannot reset them. A successful exact stop advances the supervised phase; successful exact replacement with local control proof disarms it. Central ACK loss alone cannot trigger reboot when local operational control is healthy. The precise accepted local control proof belongs in the port's review, not a loose heartbeat boolean.

At the deadline, HostCore records one recovery reboot intent with the original operation and reason, retains bounded sanitized observations, and invokes the existing systemd reboot driver. It reports the recovery cause to Central when transport is available, but reporting is not a prerequisite that could strand the device. Local dispatch/driver failure remains explicitly observable; a stuck kernel/PID1 cannot be promised to reboot without hardware watchdog qualification. Do not silently add reboot-force, direct hardware reset or an unbounded retry loop.

The first implementation's anti-loop rule is one automatic reboot for this admitted online operation in its original boot. HostCore's same-boot durable journal prevents reissue across its own restarts. After reboot the old operation cannot be replayed against a new boot; Central retains its attempt/drain and frozen target/fallback obligations, and recovery boot uses the existing exact accepted boot/fallback policy. A cold-start failure stays `recovery_required`; it does not recursively arm this online-operation reboot rule. Central must refuse re-admitting the unresolved operation as a new online attempt until its ordinary exact reconciliation/quarantine policy resolves it. This uses diskless boot identity and retained Central attempts; it does not assume `/run` survives reboot or introduce local disk storage. General repeated reboot-on-cold-failure policy would require a separate cross-boot budget and is not included.

Add deterministic supervisor tests for registration-before-dispatch, immutable duplicate registration, absent/wrong peer, broker/manager death, local observation continuing during network failure, exact phase advance and disarm, deadline inheritance across repeated HostCore crashes, intent before reboot dispatch, duplicate suppression and new-boot refusal of the old obligation. Test successful local control with unreachable Central does not reboot; unrelated healthy playback/network outages never arm it. Test a stuck old process reaches one reboot request, late stop completion before escalation cancels the still-undispatched escalation, and a recorded reboot intent cannot be turned into a second replacement attempt. Actual PID1 qualification may intercept the reboot driver in a clearly labeled fixture to verify intent and dispatch; that is not evidence of physical reboot, watchdog recovery or post-boot pixels.

## Concrete shared port for the next implementation handoff

Proposed new `appliance/apps/stop_operation.py` owns these platform-independent types; it does not import Linux or Central:

```python
@dataclass(frozen=True)
class StopRequest:
    operation_id: UUID
    boot_id: UUID
    command_sha256: str
    old: RunningApp
    dispatch_not_after_boottime_ms: int

@dataclass(frozen=True)
class StopCompleted:
    request: StopRequest
    observed_boottime_ms: int

class StopOperation(Protocol):
    def done(self) -> bool: ...
    def result(self) -> StopCompleted: ...
    # result raises a standard not-ready exception while pending,
    # or StopGuaranteeUnavailable(code, diagnostic_id) on terminal failure.

class StopDriver(Protocol):
    def stop(self, request: StopRequest, *, reattach_only: bool) -> StopOperation: ...
    def service(self, *, now_ms: int, budget_ms: int) -> None: ...
```

`reattach_only=True` can never dispatch. Fresh admission uses false once, after the lifecycle intent is durable and the recovery supervisor has acknowledged its obligation. Internally the adapter deduplicates either call against the complete immutable request. The result intentionally excludes Linux samples: its guarantee is defined above; the adapter retains evidence for audit. A caller does not mutate the returned object or its completion state. Internal adapter journal schema 1 contains the canonical request, captured Linux identity, dispatch state (`not_dispatched`, `dispatch_unknown`, `acknowledged`), completion or typed terminal fault, and bounded diagnostic counters. Do not persist open-descriptor numbers as recoverable identities. Unknown/older journal schemas refuse new effects.

A separate recovery port belongs to HostCore ownership: `arm(RecoveryObligation)->RecoveryReceipt`, `advance(ExactRecoveryProgress)->None`, and independent `service(now_ms)`. The immutable obligation contains boot/operation/request digest, old identity, exact target/fallback refs, stop deadline, restoration deadline and policy revision. HostCore validates limits against its base policy; caller-supplied deadlines cannot expand them. Progress binds the receipt and operation, contains typed stopped/replacement-control proof, and cannot extend either deadline. The transport serializes these bounded types with schema 1, authenticates the dedicated root broker peer and rejects conflicting replays. HostCore alone writes the recovery record and invokes the reboot driver. Local recovery receipts are never encoded as Central operator commands or used as app-effect permits.

Suggested disjoint leaves after review: validator owns the shared stop port plus online broker/runner consumption and deterministic integration tests; Linux owner owns process_linux adapter and platform tests; a separate HostCore leaf owns recovery types/domain/transport, HostRunner integration and supervisor tests. Agree the recovery proof and base policy values before that leaf writes code. Unit/service sandbox changes need their own integrated review because the existing HostCore boundary excludes broker storage. Root integrates, reviews authority invariants and rebuilds artifacts. No leaf may implement this proposal during the current checkpoint wrap-up.

Checkpoint handoff: proposal saved; relative documentation links checked across 140 Markdown documents. No production changes or new runtime tests were performed for this proposal. The latest Linux fixture fixes were reported ready but have not received this agent's independent re-review, and the exact diagnostic reproducer remains outstanding. Preserve the existing full-base evidence as source-specific historical results. Resume with shared-port/recovery-policy agreement, then delegated implementation, deterministic checks and refreshed exact PID1/base artifacts; do not present the design as a completed recovery mechanism.
