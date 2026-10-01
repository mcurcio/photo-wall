# Current Central handoff status

**Superseded in part 2026-10-01:** stop permits, Runtime drains, no-effect revalidation, boot-claim CAS/selection and Central-mapped node-clock deadlines described here were removed; see the [node domain model](../player-node-domain-model.md#refinement-record). This record remains history for the revision it exercised.

This support document preserves an earlier audit and its resumption checkpoint below.
**Current state supersedes the earlier unapplied labels:** migration060 repair is
applied with five passing PostgreSQL regressions. Display withdrawal repair is
applied with seven focused passes and packaged composed scenario5882 passing;
Display owns the definitive later evidence and main handoff. Linux has continued
repairs since the earlier audit: consult the main handoff and Linux lane evidence
for current applied/tested state; the historical list below is not a current Linux
status assertion.

Central source is frozen for the user's requested fresh-session pause. No Central
execution handles remain. No new test or implementation is part of this handoff.
CI revocation/gate/schema change, reboot receipt-carrier change, migration062,
guard service/live publisher integration and opt-in IaC wiring remain UNAPPLIED.
Separate explicit approvals for CI boundary, carrier authentication boundary and
full guard ledger/gate API/IaC source-plus-local-tests scope were requested by the
parent and remain pending; do not infer approval from usage-limit resolution or
from a different scope. D16 bound Run/Actuator/Program policy remains unanswered.
Persistent privileged CI and automatic cache reclamation permissions are also
unanswered. The parent records exact user questions in the main conversation;
this document records their scopes without inventing quotations.

Local tracking issue **iac-g9ut** is claimed in clean
`/Users/matt/.codex/worktrees/2dff/iac`. No guard/IaC source writes, push/sync or
deployment occurred. Do not touch the unrelated dirty IaC checkout.

Earlier automatic review usage-limit failure was resolved; subsequent CI and
carrier writes were rejected before execution on explicit authorization/risk
grounds. No rejected mutation was retried or bypassed. Proposals linked below are
review material only, not applied patches or permission.

- [CI revocation proposal](2026-09-30-node-ci-revocation-proposal.md)
- [Reboot carrier proposal](2026-09-30-node-reboot-carrier-proposal.md)
- [Guard implementation proposal](2026-09-30-node-guard-proposal.md)

## Preserved audit and chronology

# Player node current-source handoff — 2026-09-30

Target: `/Users/matt/.codex/worktrees/readiness-design/photo-wall`. Shared uncommitted work; preserve all owners. This is an audit, not a release or goal-completion claim. No live Central build/test handles remain. No failed protected edit was retried or bypassed. Migration062, guard owner ports/service, and IaC wiring are NOT applied.

## Implemented source and composition

- Canonical node contracts, durable producer/session evidence, dedupe and atomic snapshots; response receipts separate from physical effects. Overlapping boot claims retain observational transport while command admission fails closed; explicit operator CAS selects a claim without asserting physical identity.
- Reboot initiation is at-risk evidence only. Exact affirmative linked process/surface losses reconcile Output contributions within continuing Runs. Unrelated Frames/Actuators remain unaffected. Explicit current handoff authorization plus native completion is required for recovery; presentation alone is insufficient.
- Cold V2 immutable boot offers, explicit no-app path, exact base/manager/environment references, bounded streaming artifact validation/cache leases, immutable release manifest/catalog publication, additive CI packaging. Legacy protocol/factory remains separate.
- Online unbound staging and short stop permits, durable exact effect journal, fallback acceptance scoped to current cohort, sealed no-effect revalidation, immutable recovery-only lost-permit receipts, atomic pre-permit stage cancellation. Bound D16 effects remain refused.
- Qualification uses advancing current control/readiness and actual rendered assignment/variant/Frame witness. Explicit representative qualification scenario is separate from general authored-black playback. Manager preparation telemetry is command-free.
- Native Frame-v3 authority, exact withdrawal, Trial latest-presentation Save through Registry, sticky V2 calibration capability work, operator status/reboot/boot-selection UI are display-owned. Latest composed packaged run proved real normal-media witness but exposed the withdrawal race listed below.
- `central/fleet/node_routes.py` mounts node/operator API families; `central.node_app:create_app` provides opt-in composition. `central.app` composes Runtime reconciliation and lifecycle worker. Defaults remain closed/unavailable for effects; startup does not certify deployment.
- Concrete read-only Kubernetes topology/image verifier and signed evidence replay floor exist. Exact-image CI harness, strict qualification codec/signature/report validation, durable publisher/revocation ledger061 exist. Actual current-image qualification job has NOT run; fixture signing does not qualify an image.

## Required source repairs — release blockers

1. **Migration060 loses historical unresolved fences.** Current migration fills old `frame_id` with empty string while new reads require exact Frame. Backfill only a UNIQUE exact historical observation-event linkage (cause, producer, player/epoch/output/binding/time); never guess from current Registry. Abort the entire migration for any unresolved loss without unique mapping. Resolved historical records may retain sentinel. Required upgrade tests: known linkage, missing/ambiguous mapping, duplicate evidence, mixed resolved/unresolved rows, rollback with no partial migration. Not applied.
2. **Broker socket capability.** Unit lacks CAP_CHOWN for socket group10004 change. Narrow capability fix and actual restricted-unit startup test required. Not applied.
3. **Retained-app proof socket inode.** Individual file bind retains obsolete inode after broker restart. Use dedicated root-owned public IPC directory containing socket only, bound read-only into app; keep private journal separate. Test retained-app reconnection and parent/symlink/peer rejection. Not applied.
4. **XDG and DRM unit composition.** App needs own UID10004 mode0700 bounded runtime tmpfs distinct from public proof directory; Weston runtime0700. Rootless UID10005 DRM service needs existing logind/tty1/PAM composition, not seatd. Actual restricted PID1 launch remains required. Not applied.
5. **Independent HostCore supervisory observations.** Base must observe manager supervisor/exhausted-budget and Display unit state. Manager self-telemetry is not substitute; unit active alone cannot imply available. Not applied.
6. **Host command liveness.** HostRunner currently replays up to1024 retained records before command poll. Add durable acknowledgement/fair bounded delivery schedule, keep anti-replay journal and effect fences, poll commands independently of evidence backlog. Tests cover slow/failed transport, late-created event after response ACK, restart, full journal and at-most-once recording reboot driver. Not applied.
7. **CI revocation integration.** Peer review confirms new revoked qualification is persisted but existing active live CI record can admit effects until <=300s expiry. There is no authorized grace contract. Integrate revocation transactionally with closing NEW-admission gate and durable higher-generation revoked publication; retain issued immutable routing holds/rollback barriers and never retroactively retract past effect authority. Add concurrent admission/revocation regression. Not applied.
8. **Save/revision→immediate unbind withdrawal race.** Packaged run11751 established revision3 actual media, then unbound before next upload while stored snapshot was revision2. Exact revision3 withdrawal is refused as `display_withdrawal_previous_unknown`, preventing progress. Approved correction: same producer/output/full Surface immutable issued revision/handoff authorization plus fresh exact native receipt establishes OLD-role knowledge for withdrawal only; current Registry/Runtime port still decides. No new role/grant/recovery. Display owns repair; its final attempt outcome is reported separately to parent.

## Remaining D17 implementation — NOT applied

Migration062 and Photo Wall guard service/domain remain absent. Existing gate lacks proposed transactional `close_in`/barrier ports. Live CI publisher has no executable ConfigMap transport/revocation producer composition. IaC opt-in wiring is absent; local issue `iac-g9ut` was created/claimed by parent only, no sync/push/deploy.

Approved bounded design: serialize exclusive Fleet effect gate, then Coordination/Runtime, then guard row. Admission closes gate, waits existing effect transactions, refuses mutations during any issued immutable hold, and persists random mutation ticket before allowing request. Hold issuance refuses pending/unknown mutations, verifies current complete topology/exact serving+rollback images and signed CI coverage, and commits exact signed bytes/expiry before publication. Duplicate requests return same bytes without renewal.

AdmissionReview UID is NOT auditID. Exact supported Kubernetes1.35.8 dispatcher prefixes webhook audit annotations with webhook name. Join authenticated ResponseComplete using unpredictable persisted webhook-prefixed ticket plus full stored request match. Missing/ambiguous/conflicting terminal outcome remains pending. Terminal receipt alone grants no authority; fresh post-terminal topology required. Narrow evidence-record updates avoid self-dependency without generic bypass. Finite explicit route/resource/RBAC/webhook coverage, fail-closed bootstrap/FailurePolicy, and unsupported exposure refusal are required. Signatures authenticate claims; they do not implement mutation enforcement. Cluster-admin/control-plane bypass is outside enforceable claim.

Photo Wall owns service/domain; clean `/Users/matt/.codex/worktrees/2dff/iac` owns wiring only. Dirty other IaC checkout untouched. The add-workload skill and repository guidance were read; issue/review gates apply. No deployment authorized.

## Verification evidence and limits

Central completed scoped checks:16 Runtime/acceptance;12 lifecycle/codec;8 node/operator projection;3 CI publisher/signature plus83 release-plan tests passed in a combined run that also had one classification failure. Classification fixed, its targeted test passed; combined suite was NOT rerun. Last read-only AST7, scoped ruff and diff whitespace checks passed. Earlier leaf suites remain evidence for their scope only. DB checks used disposable schemas in existing local Compose database, not production.

Full repository pytest, local DB suite, lint/import/docs and migration-upgrade audit are not complete. Required after source stabilizes, not repeatedly while known blockers remain. Exact-image harness mounts test tooling only, checks installed `/app` imports/interpreter/digest/config identity and strict report hashes; actual image run/signing remains unqualified without configured distinct authority and successful matrix.

Display reports native18, current-source GTK composed20.44s, and browser stale-CAS/uncertain-reboot exact retry evidence. Latest packaged11751 proved real rendered-media tag but terminated on new withdrawal race, so NOT a passing whole scenario. Prior14338 also reached media witness but fixture count assertion prevented completion. Do not report either as fully passing.

Linux current coherent Frame-v3 artifacts pass exact archive/member/source/native provenance verification. They are now PRE-FIX milestones due source defects2–6, not final release artifacts. No matching full production squashfs/PXE run, current whole-system PID1 effect/fallback/no-effect, combined8GiB pressure, or physical DRM timing/continuity qualification exists. Base fixes require new ABI/provenance and coherent reseal; recheck current runtime inputs before accepting final packages.

Detailed Linux audit: `/private/tmp/node-linux-full-scope-audit.md`; artifact checkpoint: `/private/tmp/node-frame3-verification-checkpoint.md`; actual member verification: `/private/tmp/photo-wall-node-frame3-review/artifact-verification.json`.

## Policy/external blockers and resumption

D16 bound Run/Actuator/Programs policy unanswered: preserve explicit refusal. Persistent privileged CI and automatic cache reclamation authorization unanswered: do not infer consent. Real guard/bootstrap/serving+rollback qualification and hardware evidence remain external qualification, distinct from code we can still implement.

Automatic approval review failed due its usage limit before protected writes executed. This was a review-service failure, not a safety judgment and not agent-slot unavailability. Do not repeat equivalent writes or bypass review. Parent explicitly limited remaining work to read-only audit. After this handoff, no meaningful safe independent Central checks remain; repairs, guard implementation and final dependent builds require that blocking condition to change. No live Central or Linux handles; display final repair status must be checked separately.


## Resumption checkpoint (reviewer available, later safety rejection)

- Migration060 historical-frame repair is now APPLIED. Five focused real PostgreSQL tests passed: unique original cause mapping, duplicate matching evidence, resolved audit sentinel, ambiguous/missing/wrong cause and full rollback. This supersedes item1's prior unapplied status.
- Display narrow prior-authorization withdrawal repair is APPLIED and independently read-reviewed. Seven focused DB checks plus packaged composed GTK/media scenario5882 passed (22.46s), including Save revision promotion then immediate unbind before next upload. Actual frozen native/Player bytes unchanged; this supersedes item8's prior status. Linux base fixes still govern final artifact qualification.
- A reviewed request to modify CI revocation, rollout gate transactional closure/veto and schema061 was REJECTED BEFORE execution as an insufficiently explicitly authorized, broad security/control-boundary mutation. This is a safety/authorization rejection distinct from the earlier usage limit. No equivalent writes were retried or bypassed. Parent asked user for explicit CI boundary approval; pending. All CI repair remains UNAPPLIED.
- NodeIngest read-only cross-review found reboot events accept renewed same-producer carrier, while original reboot response requires carrier session == original command session, preventing delayed receipt recovery after renewal. Proposed narrow repair: permit renewed transport only with same original producer/scope/exact stored command, retain original payload/session identity, no new command authority. Linux preserves current refusal pending Central canonical change.
- Guard implementation depends on the rejected owner gate transaction port. Prepare design/diff without applying; do not indirectly implement the blocked gate change through guard service. No live Central handles.
