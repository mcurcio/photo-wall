# Owned stop and bounded recovery implementation — 2026-09-30

Status: implementation in progress on PR #39; not release or hardware qualification.
This resumes the [saved checkpoint](2026-09-30-player-node-resumption.md) and
implements the [stop/recovery proposal](2026-09-30-node-stop-observation-proposal.md).
The user selected single-session implementation without delegation.

## Starting identity

All 773 saved non-document paths matched the latest harness inventory before edits.
The saved components manifest still hashes to
`3f6d1997dc1e88a5da3e8b0692967fd0437cab970d89327ea37e15ee481852de`;
Player and manager archives still match their recorded environment identities.
These historical artifacts do not qualify the new source.

## Implemented boundary

The Linux adapter owns a durable stop operation and cooperative subprocess
observation. Pending or unreadable observations do not resend stop or authorize
replacement. Reattachment cannot dispatch. The broker consumes exact completion
into its existing stopped/target/fallback event sequence, independently of network
session success. HostCore owns a separate recovery journal and root-only local
packet port; its acknowledgment precedes stop dispatch. Duplicate registration
cannot extend deadlines, and reboot intent is durable before the existing driver
is invoked. A restart cannot repeat that request.

The shared policy pins the Player unit's TimeoutStopSec to 30 seconds. The initial
stop budget is two such intervals plus a 10-second observation margin; restoration
adds 60 seconds for the existing bounded target/fallback launches and local proof.
These are implemented policy bounds, not measured hardware guarantees. The broker
captures and checks the effective timeout before dispatch. Local control means a
fresh challenge response from the kernel-authenticated exact replacement process,
confirmed independently by HostCore against PID1. It is distinct from Central
receipt acceptance and visible output. No Central admission, seal, permit renewal,
D16/D17, deployment or physical reboot boundary is changed.

## Checkpoints

First focused regression run: **81 passed in 2.38 seconds**, including adapter,
broker, HostCore and packaging-boundary checks. Remaining acceptance includes
expanded crash/transport tests, broad portable and applicable DB checks, actual
PID1 scenarios with rebuilt exact components, full-squashfs integration and
physical/PXE/display qualification. No new artifact or hardware result is claimed.


## Integrated review checkpoint

The second checkpoint integrates preserved concurrent edits after the user stopped
the other writer. Stop admission now returns before any PID1 I/O; cooperative
service owns capture, dispatch and observation. The broker persists stop intent,
request identity and immutable recovery deadlines in one write. Read-only operation
views keep their original identity. Lost Host acknowledgments prevent dispatch;
replay cannot renew deadlines or bypass a recorded reboot intent. The adapter
retains its cgroup descriptor through transient errors and never requires live
root metadata after capture. Corrupt journal fields refuse completion.

Local challenge identity survives Central session expiry. An exact-process
challenge response is persisted before HTTP publication, so lost Central ACKs
cannot erase local control evidence. HostCore reports recovery counters and a
sanitized recovery fault through existing telemetry; that reporting never gates
reboot dispatch. Reboot-driver failure remains explicitly unknown with no automatic
second dispatch, consistent with the selected one-attempt policy.

Before integration, the first source checkpoint passed 2,996 portable tests
(1,282 skips, 23 warnings, 190.78 seconds) and 112 scoped PostgreSQL tests
(one warning, 30.56 seconds). A later run passed 3,003 portable tests and 119 scoped
DB tests, but overlapping source edits prevent treating those runs as final-source
qualification. The integrated adapter/broker/HostCore set subsequently passed
98 tests in 2.50 seconds; further boundary tests and stable-source validation follow.

Four real Linux credential-packet tests passed in 0.077 seconds in a networkless,
read-only arm64 container based on image
`sha256:90c772d80b3d8a6c0b733e3fdd4464e14e83bc331dc757d828c0b4b8e57cef8c`.
They exercise kernel credentials and real Unix sockets, with fixture peer-policy
and process observers. They are not an actual PID1 lifecycle or physical reboot
qualification. The literal local DB wrapper was attempted and still fails for
missing `.env`; the reviewed isolated-schema helper supplies supplemental coverage.
