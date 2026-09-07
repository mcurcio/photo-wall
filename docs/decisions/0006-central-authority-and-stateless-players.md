# Central authority and stateless Players

Status: accepted, 2026-09-06.
Owner: orchestrator.

## Decision

All durable Photo Wall state belongs to central components. PostgreSQL owns Installation records and operator intent, Runtime and Planner state, execution coordination, media publication/references, Procrastinate jobs, equipment recognition, and release/trial policy. Central domains expose named application operations and transaction-bound repository ports. Cross-domain callers do not use another domain's private methods or SQL.

Players have no retained identity, database, execution journal, authoritative cache index, update slots, or rollback state. Every Player process generates a fresh key, enrolls against its current central boot ticket, receives a new authority epoch, and reconstructs configuration and assignments from central. The Executor is the sole local authority owner and rejects earlier epochs.

A Player cache is a bounded optimization. Pins and index data live in memory. A configured directory may survive, but files are reused only after validating the exact content-addressed name, byte length, and SHA-256 against a current authorized variant. Missing or corrupt content invalidates readiness and is reacquired without changing a centrally secured assignment.

On the trusted provisioning LAN, serial, MAC, and similar observations may help central match returning equipment to its persistent equipment record. They are not cryptographic identity and never overwrite operator bindings. Recognized equipment receives its current centrally assigned Frames through fresh enrollment. Unknown equipment remains unbound. Replacing equipment requires an explicit central binding change.

Cold boot requires reachable trusted time, release/provisioning, enrollment, control, and media services. A running Player may preserve already authorized output through a temporary outage within its current lease. There is no offline cold-boot playback promise.

Media requests and Procrastinate deferral commit in one caller-owned Psycopg transaction. Procrastinate owns dispatch, task attempts, retry timing, and worker liveness. Photo Wall retains only domain publication recovery, capacity reservations, and stale-attempt fencing; it has no second polling, retry, or heartbeat scheduler.

Signed root images remain immutable central artifacts under the fixed bootstrap/kernel ABI. PostgreSQL stores accepted/candidate releases, boot attempts, consumed trials, and health evidence. Central consumes a candidate trial before issuing its ticket and makes repeated requests for the same boot idempotent. Promotion requires sustained fresh health from the exact current boot ticket and Player epoch. A volatile watchdog reboots a failed trial; the next PXE request receives the centrally accepted release. No local A/B state participates.

The Player clock mapping uses authenticated `/v1/player/time` samples independent of control-state delivery. The Player retains the established uncertainty and drift thresholds and publishes RTT, offset, application delay, drift, mapping age, clock-step, and rejection diagnostics.

## Consequences

Central availability is now an explicit cold-boot prerequisite. PostgreSQL backup and central service recovery protect more state, while Player replacement and reimaging become simpler. Cache deletion and Player storage failure no longer threaten identity or release recovery. Returning hardware recovery relies on the trusted provisioning observation mapping; a deployment that cannot protect that LAN needs a stronger equipment-attestation mechanism before broadening its trust boundary.

The Player-only package must exclude Psycopg, PostgreSQL schema code, Procrastinate, central/media implementations, and upstream integrations. Import-boundary checks enforce this alongside the package allowlist.

Earlier durable-key/PWSTATE rules in [decision 0002](0002-registry-and-enrollment.md), local continuation language in [decision 0001](0001-mvp-time-recovery-and-module-contracts.md), local worker scheduling in [decision 0004](0004-media-publication-and-worker-recovery.md), and Player-local A/B/storage rules in [decision 0005](0005-native-platform-and-registration-fallback.md) are superseded by this decision. Their dated evidence remains evidence for the exact code that ran, not acceptance of the replacement architecture.

## Acceptance boundary

Unit and PostgreSQL tests must cover transaction races, atomic task defer, queue retries, publication fences, fresh-session reconciliation, cache validation/reacquisition, idempotent boot selection, consumed trials, and stale health. Delivery still requires the complete two-Player/three-Output demo, authenticated operator walkthrough, exact-image native/cache/rollback tests, physical Pi PXE/replacement/dual-HDMI/continuity/visible-coordination qualification, and independent final review. No software-only result establishes those physical claims.
