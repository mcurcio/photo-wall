# Secured content across Player restart — 2026-09-06

An independent integrated review found that central discarded unexpired
offers and assignment locks solely because a same-key Player had re-enrolled
with a new authority epoch. With an unchanged Frame binding but changed or
empty source membership, it could select different bytes or no content for
the same existing Run/assignment. This violated the accepted content-lock
contract. The historical full-media pass did not cover membership mutation
between securing an assignment and restarting that Player.

The correction uses still-live historical offers and confirmed locks only as
content constraints, rechecking the current enabled Frame/Output/generation.
New-epoch plan selection, revision numbering, offer limits, delivery, readiness
and commitments remain isolated from the prior epoch. Fresh readiness is
required even if an old commitment existed. Conflicting historical content
records retain the existing fail-closed rejection; the fix does not silently
choose between already divergent variants.

The finalized initial seven PostgreSQL regression scenarios produced **five
failures and two passes before the production change**. After the fix, those
passed. Two additional cases establish that previously committed old-epoch
work cannot regain execution authority. Together with existing coordination
cases, **22 focused tests passed in 8.91s**, with one dependency warning.
The tests cover changed/empty membership, offered/secured/committed content,
stale token/readiness rejection, fresh readiness, current-epoch offer limits,
actual lease expiry and changed binding generations. An independent strong
review confirmed the final boundary and separately exercised conflicting locks,
disabled Outputs, stale generations and expired layers; no residual material
finding remained in that scope.

The [coordination contract](../module-coordination.md) distinguishes content
identity from execution authority. Full-revision service and image benchmarks
remain separately tracked; these PostgreSQL/domain checks do not qualify
physical display continuity or image rollback.

The full PostgreSQL-backed suite passed **851 tests / 15 explicit host and
opt-in integration skips / 4 dependency warnings in 101.64s**. Ruff, all
57 documentation link sets and whitespace checks passed. Five skipped
systemd update scenarios were separately executed on Linux in their dated
evidence; this suite does not relabel those host skips as new runs.
