> Historical pre-repair audit/checkpoint. Its unapplied status and capability mask are superseded by the [stable Linux checkpoint](../2026-09-30-node-linux-integration.md#stable-pause-checkpoint-after-linux-unit-repairs) and [main handoff](../2026-09-30-player-node-handoff.md). This is retained for provenance, not current instructions.

## Matching-artifact verification checkpoint

Existing fixture build session10496 completed successfully. Image
`sha256:590ceb64a06ae4d11580b0786c37933b79edd6bbaf1580326a9df8ffb4ce294a`
contains the exact frozen base/display packages on the Frame-v3 Player dependency
image. This is a disposable PID1 fixture image, not the production squashfs.

The standalone PID1 probe was not executed. Automatic approval review could not
complete because of a usage limit; its response explicitly said this is a review
failure, not a determination that the action is unsafe. No bypass or container
startup occurred. This dependency is distinct from unanswered authorization for
persistent privileged CI execution and automatic cache reclamation.

Read-only artifact verification session25753 passed. Every manifest member was
checked against actual archive bytes: Player22411 members, manager6295. Exact outer
SHA/size, regular-file SHA/mode/size, symlink targets, dependency-lock hash and source
provenance hash matched. The sealed Player49 and manager23 source files matched the
frozen source; the app-private Frame-v3 client matched its native handoff digest.
All112 current runtime inputs still matched the frozen snapshot. Full results are
`/private/tmp/photo-wall-node-frame3-review/artifact-verification.json`.

No new live build/probe handles remain. Actual matching-artifact PID1 execution,
complete PXE cold boot, composed online effect/fallback/no-effect qualification,
and physical Pi display/pressure qualification remain distinct outstanding evidence.
