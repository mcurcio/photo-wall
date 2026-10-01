# Owned stop and bounded recovery implementation — 2026-09-30

**Superseded in part 2026-10-01:** stop permits, Runtime drains, no-effect revalidation, boot-claim CAS/selection and Central-mapped node-clock deadlines described here were removed; see the [node domain model](../player-node-domain-model.md#refinement-record). This record remains history for the revision it exercised.

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


## Stable source and rebuilt components

Commit `c07666b1b62cdf816980a7d36aaf70e0320e806b` passed:

- Portable suite: **3,016 passed, 1,286 skipped, 23 warnings in 197.81 seconds**.
  Skips retain the prior PostgreSQL/browser/published-wire/platform prerequisites,
  plus four Linux-root credential tests run separately below.
- Scoped PostgreSQL regression: **132 passed, one warning in 32.33 seconds**,
  covering stop/recovery, Linux adapters, broker, HostCore, Central lifecycle,
  current-link reconciliation and Runtime reconciliation. No DB skips.
- Focused boundary set: **101 passed in 2.47 seconds**.
- Four actual Linux credential-packet tests: **passed in 0.078 seconds**.
- Repository-wide Ruff and documentation-link checks passed before publication.

The coherent components are retained at
`/Volumes/Dock/Temp/photo-wall-stop-c07666b/components`. The clean committed source
was frozen before rebuilding; all non-document inputs matched again at build end.
The reconstruction tool retains its conservative working-tree provenance label;
it is not a release-certification tool.

| Identity | Value |
|---|---|
| Source inventory SHA256 | `643f3935a82007b51715da78d4b0bca949bf060ff389eb5458432066419066f9` |
| Components manifest SHA256 | `f06848bf78a8710c771056022dc83202c310c2c075336e446227e39cc8a778de` |
| Base ABI | `node-v2-efcfc4b30319d94327048ad79c47c362f221494ed23bd1c4fe9908c43ea41da5` |
| Player environment | `2f246f7c7ce936a3d70e5cb4a95bba7ec066529f743e2bbc33d8b15aa7835d7c` |
| Manager environment | `6142353f01e335a7b365bcf9052ee8a00c2f6329cad2777a2321ec4439534bbc` |
| PID1 fixture image | `sha256:86a1e0f2dfe2bd8a71dc8cae2735268aedc58a5f4fbcb138dc7c1156a899614f` |

Independent reopening verified 22,412 Player archive members, 6,296 manager
members, their 49/23 source files, and all 153 regular base-package members.
The unchanged native artifact was independently rehashed before reuse.
[Compact verification](player-node-handoff-support/owned-stop/component-verification.json)
is preserved with the executed recipes:
[components](player-node-handoff-support/owned-stop/build-components.txt),
[verifier](player-node-handoff-support/owned-stop/verify-components.txt),
[targets](player-node-handoff-support/owned-stop/build-targets.txt),
[PID1 image](player-node-handoff-support/owned-stop/build-pid1-image.txt), and
[scenario runner](player-node-handoff-support/owned-stop/run-qualification.txt).
All old artifact directories remain unchanged.

The first actual PID1 cold/update success case passed **one test, two deselected,
four warnings in 193.74 seconds**. Its ordered effects were `intent_stop`, `stopped`,
`starting_new`, `running`; exact final process verification and natural Central
operational discharge passed. Container cleanup completed. This exercises the
installed packages with synthetic hardware; full production squashfs, physical
reboot/PXE/DRM/HDMI and pressure qualification remain distinct.
