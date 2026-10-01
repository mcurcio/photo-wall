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
