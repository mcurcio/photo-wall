# Node Central integration checkpoint — 2026-09-30

This is implementation evidence, not deployment or physical qualification. The full
Player node objective remains active. Production rollout gates remain closed.

## Implemented source

- Canonical stdlib command/session, telemetry, process-link proof, and frozen V2
  boot contracts are in `contracts/node_commands.py`, `node_observation.py`,
  `node_app_link.py`, and `node_boot.py`.
- Durable adapters and mounted opt-in routes live in `central/fleet/node_*.py`;
  migrations 053 and 054 own evidence dedupe, atomic per-fact snapshot projection,
  separate response/effect storage, scoped credentials, immutable cold offers,
  exact deployment roots, and audited boot selection.
- `central/node_runtime_reconciliation.py` reconciles affirmative linked process
  and Output loss through Coordinator. It preserves the Run, unrelated Frames,
  and Actuators. Reboot initiation never invalidates execution authority.
- `Coordinator.display_admission_in` makes an explicit current-link/current-Output
  decision under Runtime locks. An active legacy drain is refused; V1 attempts
  cannot authorize a V2 target. Presentation alone cannot release an Output loss.
- `player/node_app_link.py` and the Player identity/service adapter submit the
  canonical signed receipt/process link through the protected broker socket.
- Sealed environment bytes use the existing single-writer content worker, exact
  immutable asset references, digest checks, and descriptor stream leases.

## Admission and uncertainty

A LAN serial claim is operational evidence, not authenticated physical identity.
Legacy offer adoption yields observation credentials with command eligibility
false. V2 transport credentials also remain usable during ambiguity, while every
command creation and poll rechecks the durable selected-boot fence atomically.
Overlapping unexpired grants do not elect a physical boot by arrival. An explicit
operator CAS can select a logical boot with an immutable audit reference; expiry
only closes admission authority and does not prove the old physical Pi stopped.
Already delivered effects remain uncertain until independent evidence or expiry.
Manager primary and optional fallback are immutable properties of the base digest.
A no-app offer is explicit; absent artifacts do not pretend bytes are available.

## Validation and current boundary

Local development PostgreSQL is reachable through the existing Compose database.
Tests use random disposable schemas, never a deployed schema. Focused node/protocol
checks passed 18 tests; Runtime plus existing coordination checks passed 18 tests.
After migration 054, cold offers, ambiguity, node routes and existing cache handlers
passed 25 tests. The initial expected failures used legacy offers for command tests;
those fixtures now publish genuine V2 deployments. One Starlette deprecation warning
remains. No hardware or deployment result is claimed.

The subsequent 25-test run also passed exact sealed-environment worker/stream lease,
display admission and first-frame non-release checks. The opt-in factory and cold
route suite passed 13 tests.

Next: integrate DisplayHost
and CalibrationTrial routes owned by the display lane, then implement the V2 online
lifecycle target reservation/effects and operator UI. D16 bound-update policy is
still an external decision, not silently chosen here. Whole-suite, docs and final
local integration checks remain due after the shared implementation stabilizes.
No background process was active at this checkpoint.

## Active online leaf checkpoint

The shared `contracts/node_lifecycle.py` stage/readiness/stop-permit/effect codec
is frozen for the Linux owner; one portable round-trip/adversarial codec test
passes. Migration 056 now defines the V2 operation, readiness, separate response
and effect, immutable drain, append-only discharge, revalidation, explicit
qualification and scoped acceptance ledgers. Central stage/permit/effect/acceptance
adapters are not yet implemented at this checkpoint. They must not be reported
as runnable merely because their tables/contracts exist.

The local migration smoke process is PTY handle `31336`, running
`tests/test_node_boot.py` through `/tmp/photo-wall-node-tests.py`. No build was
restarted. The preceding opt-in factory/cold suite passed 13 tests. The DisplayHost
route is mounted; peer review identified completion across session renewal, which
the display owner is repairing. Runtime display fences now use semantic process,
boot, Registry and Output identity so a fresh proof of the same process does not
spuriously change authority.

Remaining online interfaces: independently authenticated AppManager read-only
desired/artifact access; broker command/readiness/permit/effect/no-effect exchange;
truthful explicit representative-media qualification with genuinely new control,
readiness and native-buffer witnesses over a bounded sustained window; fallback
admission rechecks exact base/ABI and Output/mode/capacity cohort. No accepted
fallback is fabricated from cold selection, a quiet app or a synthetic frame.
Missing eligibility permits bounded staging but refuses withdrawal. D16 has no
answer yet, so bound withdrawal stays unavailable.

## Composed online and verifier checkpoint

The earlier source-only online checkpoint is superseded here. Migration 056 and
`node_lifecycle.py` now back mounted stage, manager desired/artifact, broker
readiness/permit, separate response/effect, revalidation and no-effect APIs.
`node_acceptance.py` implements explicit representative-media qualification;
qualification begin/sample routes are mounted. Display exchange and CalibrationTrial
begin/edit/status/save/end routes are mounted, with Registry-owned atomic Save.
The scheduler consumes affirmative unbound target/fallback evidence. It does not
mint rollback acceptance from a quiet target or clear a fence from first frame.

Eight focused lifecycle/codec tests passed, including delayed pre-expiry quiescence,
a missing journal sequence, cached control proof rejection, immutable replay,
intent/discharge concurrency and lost permit response across freshness/expiry.
No-effect requires one terminal executor seal and the complete journal through its
watermark. The separate recovery-only permit receipt cannot enter the executable
permit parser. Missing accepted fallback and bound withdrawal refuse admission.
The acceptance used in these command tests is explicitly a fixture; the sustained
qualification writer still needs its positive owner-evidence integration test.

A read-only Kubernetes adapter now resolves actual Pod/ReplicaSet/Deployment and
registry-qualified running image identities, inventories direct Services and
EndpointSlices plus supported routing APIs, and verifies distinct CI/deployment
signatures. Migration 057 persists anti-replay generations, authority fingerprints
and record UIDs. Unknown routing APIs, partial inventories, inaccessible resources,
changed topology, old/revoked records and unqualified exact images refuse. The
measured scope must match the persistent gate even if the image did not change.
Seventeen focused verifier and node-route tests passed after correcting named
refusal error propagation. An earlier combined run had 27 passes and two error-code
assertion failures; both remained fail-closed and were fixed in the rerun.

The concrete adapter does **not** implement the external deployment mutation guard.
Real signed release matrices, a deployed close-before-mutation controller, rollback
artifact qualification and hardware evidence remain unavailable. Configuration
alone does not open the gate. The runbook records the exact external deliverable.
No running process handle remains at this checkpoint.

The canonical V2 release manifest is now authored in `contracts/node_release.py`
and frozen for the Linux CI producer. Publication/catalog consumption is the next
cold-path leaf; direct operator publication is currently implemented and tested,
while automated sealed V2 release ingestion must not yet be claimed complete.

## Publication and qualification verification checkpoint

The canonical producer output now traverses the actual GitHub origin parser,
immutable V2 catalog (058), streamed artifact hash/size verification, exact
NodeDeployment publication and explicit selection. Four publication tests passed,
including actual `package_release_artifacts` output rather than only a hand-authored
manifest. Verification uses 64 KiB transport chunks and one bounded scratch file
at a time with a free-space reserve; serving cache ownership remains with its
worker. A legacy-origin/corruption/lifecycle regression run passed 106 tests.
The earlier wrong expected exception for corrupt stream was corrected to preserve
the origin's existing retry classification.

Sustained qualification is now exercised through real Registry/Runtime/process-link
and Display owner adapters. Fifteen qualification/route checks passed, followed by
nine acceptance/preparation checks including actual qualified acceptance feeding
fallback selection and a later Output mode change refusing the stop permit.
The canonical normal-frame witness binds the actual renderer's visible media
assignment and variant; synthetic and Trial tags fail this qualification scenario.
Repeated control/readiness/buffer evidence cannot advance the window. A gap,
Output-mode or assignment change restarts it, and an old binding is refused.
These tests supply compositor receipts; they do not assert physical pixels.

Manager observation intake/projection is mounted with its own quota and immutable
sample retry storage (059). Lifecycle status explicitly exposes expired revalidation
as recovery_required while retaining the Runtime fence and artifact roots.
At this checkpoint no tool process remains active. The equal-counter cross-Frame
identity and explicit withdrawal correction is in progress with the Display owner;
its native/GTK tests are intentionally held until that source boundary stabilizes.

### Frame loss identity and staging cancellation checkpoint

Migration 060 preserves pre-Frame loss rows as historical empty identities and
adds exact Frame identity to all new loss keys, commit blocking and retained-plan
invalidation. Sixteen focused Runtime/acceptance PostgreSQL checks passed,
including replacement Frames with equal binding/configuration counters; old or
missing Frame evidence neither clears an existing loss nor blocks the replacement.
Twelve lifecycle/codec checks passed after adding terminal broker seal/watermark
and exact old-process context to pre-permit cancellation. Closure is an immutable
operation tombstone serialized against ready-to-stop; a missing permit read alone
does not close anything. A lost permit wins that race and must be recovered through
the separate read-only receipt and post-deadline no-effect path.

No Central process handles remain active at this checkpoint. D17 exact-image CI
producer and approved separate IaC admission guard remain implementation work;
pre-image checks cannot qualify a built digest. No deployment gate was opened.
