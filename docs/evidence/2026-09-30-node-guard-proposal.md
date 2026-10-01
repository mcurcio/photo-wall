# D17 guard implementation specification — pending boundary authorization

No source/migration/IaC edits from this specification have been applied. This is
reviewable preparation for the original close-before-mutation requirement, not
an alternate implementation of the rejected gate change.

## Ownership, composition, files

Photo Wall owns `central/fleet/node_rollout_guard.py` (domain/ledger),
`central/node_guard.py` (FastAPI factory), `central/fleet/node_evidence_publisher.py`
(exact ConfigMap outbox transport), and migration062. Reuse the existing
KubernetesReader, topology/image measurement, signed CI codec and gate owner.
No command route opens the gate from caller-provided digest claims.

Migration062 owns provisioned guard identity/head, immutable admission tickets,
immutable terminal receipts, immutable signed holds, publication delivery state,
and explicit coverage qualification. Existing061 owns CI publication/revocation.
Canonical envelopes should remain in contracts/node_rollout.py (not appliance
runtime imported); freeze signatures before consumers. No duplicate signature
domain literals. Migrations060/061 remain unshipped branch changes; never rewrite
an applied production migration checksum.

`central.node_guard:create_app` is a separate opt-in process. It requires explicit
configuration for installation/audience, namespace, protected Deployment UID,
exact webhook name, inventory coverage, ConfigMap names/UIDs, trusted CI authority,
guard private key, authenticated audit/admin identities, API CA and database.
Missing any configuration leaves readiness unavailable and effect gate closed.
The ordinary Central/node_app remains unchanged by default.

## APIs and bounded authority

- POST /admission: Kubernetes admission.k8s.io/v1 AdmissionReview, bounded body,
  authenticated API-server TLS identity, exact webhook routing. Validate request
  operation/group/resource/subresource/namespace/name/UID/user/old+new object.
  Dry-run returns no hold or mutation permission for a real request. Unknown
  covered mutation fails closed. Response echoes admission request UID only for
  AdmissionReview protocol; it is never treated as auditID.
- POST /audit: authenticated API-server audit EventList sink. Bounded batch/body,
  exact configured cluster/audience identity, ResponseComplete only for terminal
  handling. This endpoint has no admission or hold issuance capability. Missing
  receipt remains pending; it is never repaired by time elapsed.
- POST /holds: narrow configured controller identity requests an immutable hold
  by UUID and expected head revision, max300s. Server supplies observed topology,
  verifies fresh signed CI/revocation status and coverage. No request-supplied
  verified flag, arbitrary topology digest, or image identity is accepted.
- POST /close: authenticated deployment operator closes new admission through
  existing owner CAS. No retroactive command/hold revocation.
- GET /status and /ready: expose unresolved tickets/hold deadline/coverage and
  publication lag without credentials, signed material secrets, or readiness
  claims inferred from process startup.

## Lock order and immutable journal

Every mutation/hold/publication transition acquires the exclusive Fleet effect
singleton first, then Coordination and Runtime locks for barrier checks, then
its guard head row. CI publication/revocation uses gate→CI head consistently.
Effects acquire the shared Fleet gate before existing domain locks. Do not
invert ordering through callbacks or nested transactions.

Admission transaction closes gate, waits earlier effect transactions, validates
coverage and request identity, refuses while any issued hold's immutable deadline
has not elapsed, and persists an unpredictable ticket plus exact request hash
BEFORE committing allowed=true. Caller cancellation/response loss does not erase
ticket. Controller retry receives a new ticket per actual new admission request;
exact identical request UID retry returns the original decision only. Conflicting
reuse refuses. No mutation request receives an allow response before commit.

Allowed response carries auditAnnotations ticket and request_sha256. Kubernetes
1.35.8 validating dispatcher prefixes these with exact webhook name; verified
against its tagged primary source. Audit receipt must carry both prefixed fields,
match stored request identity (authenticated user, operation mapping, API resource,
subresource, namespace/name, applicable object UID), and terminal status. Do not
compare AdmissionReview UID with auditID. Admission's defaulted/converted object
is not assumed byte-equal to an audit patch body. The authenticated ticket+stored
request hash is the exact request correlation; audit identity fields are additional
checks. Duplicate identical terminal receipt is idempotent; conflicting result or
truncated/ambiguous identity stays fail-closed. Denied/error terminal result does
not automatically certify no mutation: retain terminal record and require fresh
post-terminal topology before hold issuance. No annotation itself is authority.

Hold issuance under the SAME serialized head refuses any pending/ambiguous ticket,
any live mutation, unresolved Runtime/legacy lifecycle rollback barrier, stale CI,
or unsupported coverage. Observe exact current images/routes and all relevant
endpoint/controller generations after terminal receipts. Persist signed exact
scope and mutation_not_before=expires_at BEFORE publication. Idempotent request
returns identical bytes/deadline, never renewal. Revoked qualification closes new
admission but cannot shorten already-issued hold; neither endpoint withdrawal nor
unsafe rollback becomes allowed by early revocation. After hold expiry unresolved
effect barriers still restrict unsafe image rollback. Publication failure leaves
gate closed; no process restart clears holds or pending tickets.

## Kubernetes credentials and publication

Inventory reader gets read-only get/list on explicitly enumerated API resources;
absence/partial pagination/unknown routing APIs refuse certification. Separate
publisher identity gets get/update ONLY the two precreated evidence ConfigMap
names (UID pinned, resourceVersion CAS), not create/delete arbitrary resources.
CI and guard signing keys are distinct trusted authorities; ConfigMap editing
permission is never signing authority. CI immutable evidence and fresh signed
revocations are mandatory, not optional unsigned success. No keys invented here.

Evidence-only ConfigMap writes need a narrow admission branch authenticated as
that exact publisher, exact object name/UID, metadata immutability and evidence
payload-only change. Verify configured signature/generation and durable outbox
membership before accepting; no user/service-account-wide generic bypass.
Publisher sends exact committed bytes in generation order. Delivery ACK is separate
from payload immutability; concurrent UID replacement/replay fails closed and
requires explicit reprovisioning. Do not grant writer permission to patch webhook,
RBAC, Deployment or routing resources.

## Finite enforcement coverage and bootstrap

Inventory must explicitly cover all serving/rollback Deployment→ReplicaSet→Pod
changes, relevant Pod status/admission subresources, Services/EndpointSlices,
Ingress/Gateway/route/ReferenceGrant and supported NetworkPolicy changes, and the
webhook/RBAC/configuration identities needed to keep enforcement active. Existing
reader lists cluster-wide Services/routing resources; unsupported API exposure
already refuses qualification. Coverage verification must match actual webhook
rules, failurePolicy=Fail, matchingPolicy/namespace exclusions and immutable
provisioned identities. No unverified namespace-label selector bypass. Admission
webhook's own Deployment/RBAC/config updates require explicit closed bootstrap;
never self-authorize its rollout while a hold exists.

This does not claim to constrain cluster-admin/control-plane/host changes or
physical node/network failure. Runtime and transport authority remain separate
from availability. Any unsupported alternate route, controller action outside
the covered API mutations, missing audit delivery, or unavailable enforcement
posture means no certificate. Read-only polling is never close-before-change.

Bootstrap order is explicit external deployment work: provision closed gate,
DB/schema/identities/TLS, audit sink and FailurePolicyFail webhook, complete
coverage/negative probes, then allow hold/certification. Unknown state defaults
closed. Do not enable by default or deploy from this task.

## IaC scope

Only clean `/Users/matt/.codex/worktrees/2dff/iac`, issue iac-g9ut (created/claimed).
Photo Wall workload bundle exposes opt-in guard/audit/publisher settings and
service resources; platform stack owns cluster-level webhook/RBAC/audit wiring.
Keep workload/stack layering and skill review gates; no raw platform resource
construction in workload domain. Existing deployment defaults and unsigned
release assets stay unchanged. No edits to dirty alternate IaC checkout, no
push/sync, preview/apply/deploy, privilege escalation pipeline or cache reclamation.
Audit policy/control-plane sink configuration may be external to this stack;
if no supported owner exists, explicitly leave readiness unqualified and document
that deliverable instead of silently omitting it.

## Acceptance before qualification

1. Concurrent held effect admission vs close/mutation; mutation waits and cannot
   publish ticket/allow out of order. Hold-vs-ticket winner is serialized.
2. Crash/lost response before/after journal commit; no unjournaled allow/hold.
3. Exact terminal ticket/hash correlation; wrong auditID equivalence, missing,
   truncated, duplicate and conflicting audit events never elect completion.
4. Multiobject/controller retries and denied/error outcomes require fresh topology;
   absent audit event cannot be timed out into permission.
5. Same-image topology mutation closes admission; partial/unknown inventory and
   missing webhook/audit/RBAC coverage refuse hold. Test own writer narrow exemption.
6. Revocation-vs-admission and premeasured certificate races; transport outage
   cannot reopen; issued hold deadline remains immutable; historical revoked
   artifact cannot be mislabeled as newer unrelated qualification.
7. Scoped reader/writer credentials, wrong identity/signature, UID/key replacement,
   replay and expired records fail closed. Exact rollback image coverage required.
8. Real local HTTP/database adapter composition and default-closed factory tests;
   mocked Kubernetes establishes software semantics only. Real cluster bootstrap,
   exact-image matrix and hardware qualification remain separate evidence.
