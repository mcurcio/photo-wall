# Player node resumption — 2026-09-30

This continues the [stable handoff](2026-09-30-player-node-handoff.md) in the exact
`readiness-design/photo-wall` checkout. The complete Player-node objective remains
open. No commit, deployment, production command, physical reboot or hardware
qualification was performed. The earlier handoff and inventories remain historical
records, rather than being rewritten to describe later bytes.

## Saved state and input freeze

The branch remained `codex/readiness-design`, with parent HEAD
`0a9b530caae00757ebae66b74b47adb38f1a5fb0`. Before edits, all 764 nonignored paths
outside `docs/` matched the saved inventory by bytes and mode, with canonical
entry-list SHA256 `7bee1a97c254e761fceffb0fdabf7f7f298f8aae01b3e93f43ad271439ada926`.
All 35 Linux file hashes matched; its manifest file SHA256 remained
`5a75b5f4bd21ef44c622e21c970e0f41e3876b9bfcbe56a00ad28f76e6729806`.

The frozen review and native artifact directories, their four provenance JSONs,
and all three immutable images named in the handoff were present. The native
Debian package and private bridge were independently rehashed and matched the
handoff; the graphics ABI and frame-v3 marker matched. Presence of these earlier
artifacts does not qualify the repaired final base.

The post-repair [resumption source inventory](player-node-handoff-support/resumption-source-inventory.json)
covers 765 non-doc paths. Its canonical entry-list SHA256 is
`cea5a46ceb53b0f0bfa00d7651414f6a0aa67b8cffba6d220fbc9cae3dfe8fd2`.
There are ten intentional changed/new paths relative to the handoff inventory:
two production packaging files and eight test/support files. This is a dirty-tree
snapshot, not a commit or release artifact digest.

## Repairs and focused verification

- Base dependency declarations and base ABI inputs were repaired in the Linux
  lane. Production PAM/logind dependencies must belong to the base closure;
  package declarations must affect its ABI. Linux's focused checks passed
  **80 tests in 2.46 seconds**; its evidence record owns the exact artifact and
  Linux process claims.
- The shared job catalog fixture now supplies a typed `FetchSealedEnvironment`
  handler and sample. Queue-operation checks reuse that canonical fixture while
  substituting the real rescue/purge handlers. No production catalog or dispatch
  behavior changed. Four infra modules passed **110 PostgreSQL-backed tests in
  21.04 seconds**.
- Historical migration checks seed historical stop rows as data, avoiding current
  Runtime readers against a pre-052 schema. Real `Database.migrate()` tests cover
  historical identity/fences, offer linkage, unique Frame recovery, idempotence,
  and atomic rejection of missing or ambiguous Frame history. Central's focused
  set passed **10 tests in 7.09 seconds**. No production migration was changed or
  run against deployment data.
- The existing import-order lint failure in the release artifact test was repaired.
  Repository-wide Ruff passed after all source changes.

## Baseline findings and environment limits

The first portable whole-suite run completed with **49 failed, 2,906 passed,
1,265 skipped, 23 warnings in 194.33 seconds**. Twenty failures were stale catalog
fixtures corrected above. Twenty-nine release-plan failures came from `uvx`
trying to write outside the sandbox. A cache-only retry still failed its tool
installation path (**29 failed, 55 passed in 8.23 seconds**). With all three UV
cache/tool/bin destinations under `/tmp`, the same release-plan module passed
**84 tests in 44.62 seconds** without a source fix.

The initial supplemental DB whole-suite run reached 44% and exposed the historical
stop seeding error plus catalog fixture failures. The orchestrator requested a
graceful interrupt to fix known failures before the final run. Ctrl-C produced
pytest failure traces and wrapper `KeyboardInterrupt`, ending with exit 130;
aggregate counts and elapsed time were not emitted and are not claimed. A
post-exit DB check found zero other client connections. Three pre-existing or
unattributed random test schemas remained; no before-run schema inventory existed,
so they were neither attributed to this run nor deleted. Host process inspection
was sandbox-refused. No global host-process cleanup claim is made.

The literal required `.venv/bin/python scripts/test_local.py -q` fails because this
checkout has no `.env`. No persistent credentials were created. Supplemental DB
checks use the reviewed handoff helper, read local Compose credentials in memory,
and create/drop their own random schemas. They are reported separately from the
literal command limitation.

## Completed broad checks at the first resumption freeze

The final portable whole-suite check passed: **2,956 passed, 1,270 skipped,
23 warnings in 207.35 seconds**. Its skips were 925 PostgreSQL checks without
DB configuration, 317 browser opt-in checks, ten published-package wire checks,
and 18 Linux/platform/tool/image prerequisites (five BusyBox, two seqpacket,
two initramfs-tool checks; one each for SCM_RIGHTS, SCM_CREDENTIALS, base and
bootstrapper dpkg builds, SO_PEERCRED, TFTP, immutable provenance image, abstract
sockets and device-tree tools). Warnings were library deprecations and Hypothesis
using an in-memory database because its default directory was unwritable.

The supplemental DB whole-suite check completed with exit 0: **3,877 passed,
348 skipped, one expected failure, 24 warnings in 1,004.22 seconds (16:44)**.
There were no missing-DB skips. Its 348 skips were the same 317 browser, ten
published-wire and 18 platform/tool/image checks, plus one real-GitHub-token
check, one pinned native-display-image check, and one interactive local fixture.
The expected failure is `test_a3_cache_wipe_leaves_exactly_one_media_writer` in
`tests/test_two_pods.py`: the documented 2026-09-23 legacy media flock defect,
where deleting a held lock file lets the standby lock a replacement inode.
It remains an existing limitation, not a new passing media-concurrency claim.

After both suites terminated, all 765 non-doc entries still matched
`cea5a46ceb53b0f0bfa00d7651414f6a0aa67b8cffba6d220fbc9cae3dfe8fd2`.
Subsequent unit repairs and their separate snapshot/checks are recorded below;
these broad results are not retroactively attributed to later bytes. Temporary logs are
`/tmp/photo-wall-resume-final-portable.log` and
`/tmp/photo-wall-resume-final-db.log`; temporary files are not durable evidence.

Both commands use `PYTHONDONTWRITEBYTECODE=1`, separate pytest basetemps, and
`UV_CACHE_DIR`, `UV_TOOL_DIR`, `UV_TOOL_BIN_DIR` under `/tmp`. The portable check
keeps pytest's cache under `/tmp`; the isolated DB helper disables it. These are
test-environment controls, not source or deployment changes. The host is macOS;
Linux-native, browser, privileged PID1 and hardware opt-in checks remain separate.

## Subsequent actual-unit failure and repair checkpoint

The Linux lane rebuilt and independently reopened a coherent full component set
from the first resumption freeze. This verifies base/app/manager package and
archive membership and current source closure; it does not produce a new
production squashfs/initrd/PXE bundle or certify serving images.

An actual PID1 cold-path probe then found an `EXDEV` rename in preparation:
separate `ReadWritePaths` entries made downloads and manager roots distinct mount
points inside the unit. The candidate import therefore failed before lifecycle
qualification. The standalone fixture is failed evidence, not a passing cold or
online scenario. The reviewed repair stages each selected environment inside a protected
`.cold-staging` directory in its destination app/manager root pool, so import
renames remain within the same writable mount. It rejects unsafe staging
ownership/mode and removes now-unneeded writable downloads/preparation binds.
The owning three source/test paths are `appliance/node/bootstrap.py`,
`appliance/systemd/photo-wall-node-prepare.service`, and
`tests/test_node_boot_linux.py`; no Central or migration source changed.

The separate [post-unit source inventory](player-node-handoff-support/resumption-post-unit-source-inventory.json)
contains 765 non-doc paths, canonical SHA256
`20b05d621440d29c7bc1a58fb9435250be5780250015cb3bd9835fbc41c2620a`.
The completed broad DB snapshot above remains unchanged. The post-unit scoped
PostgreSQL check passed **eight tests in 0.28 seconds**, with no skips. Full Ruff
and documentation checks passed. The full portable rerun completed with exit 0:
**2,958 passed, 1,270 skipped, 23 warnings in 198.51 seconds**. Skip categories
are unchanged from the first portable run. After termination, all 765 non-doc
entries still matched the post-unit inventory. The
Linux lane reported **69 passed, one DB skip in 2.13 seconds** before the separate
DB-backed run removed that check's missing-configuration limitation. Full component
resealing and independent archive verification have passed after the repair;
the actual-unit rerun remains pending. The
[Linux record](2026-09-30-node-linux-integration.md) owns exact build/process details.

## Restrictive-umask extraction repair

The next actual PID1 run failed both unprivileged manager and Player at
`200/CHDIR`: extraction under the unit's `UMask=0077` left `rootfs` and nested
directories at mode 0700. Earlier extraction under 0022 had hidden that defect.
The private outer digest wrapper at 0700 was correct and remains unchanged.

The reviewed shared producer/extractor helper now assigns the format's canonical
0755 mode only to real directories inside the unpublished rootfs. It uses `lstat`,
walks without following links, and chmods without following links. Absolute
in-root link bytes and exact regular-file modes remain unchanged. The producer
explicitly gives the new empty public-config mountpoint mode 0444. Verification
rejects noncanonical reused directories and a rootfs owned by the wrong UID,
rather than silently repairing an already published root.

The [directory-repair snapshot](player-node-handoff-support/resumption-directory-source-inventory.json)
contains 765 non-doc paths, SHA256
`08654028a826930e57e1b6ef758587148580305009e0524c372ea26782821ef4`.
Only `appliance/node/environment.py`, `scripts/build_app_environment.py`, and
`tests/test_node_linux_adapters.py` changed since the post-unit snapshot. Focused
PostgreSQL-backed adapter/artifact/boot checks passed **37 tests in 2.87 seconds**;
full Ruff and docs checks passed. The first portable rerun found **two failed,
2,961 passed, 1,270 skipped, 23 warnings in 197.78 seconds**. Both failures were
repository Dockerfile-ownership checks recognizing an archived documentation
recipe named `Dockerfile.txt` as a shipped build input. The text was renamed to
`tool-build-recipe.txt` without changing its bytes or weakening the tests; both
focused checks then passed **two tests in 0.26 seconds**. The clean full portable
rerun passed **2,963 tests, 1,270 skipped, 23 warnings in 187.17 seconds**.
All 765 non-doc entries still matched the directory-repair inventory at completion. New base/sealer/manager source identities require rebuilt
components; earlier roots remain historical rather than qualifying the repaired
extractor. Exact rebuilt identities are in the
[Linux directory-policy checkpoint](2026-09-30-node-linux-integration.md#actual-unit-discovery-and-directory-policy-checkpoint).

## Later actual-unit discoveries

The directory-repaired actual fixture reached a running unprivileged Player
with node linkage, but exposed two further boundaries: the private app namespace
lacked its diagnostic health-file directory, and the stop wait reused an
active-only process observer during PID1 deactivation. The real stop attempt
retained its unknown effect and drain; no forced recovery or synthetic completion
is recorded. The reviewed scope keeps the health file in an app-private tmpfs
and reserves any deactivation tolerance for a bounded stop-only observer.
The reviewed repair adds a bounded stop-only observer: surviving process, invocation,
root and cgroup identities must match; only repeated terminal observations with
no pending PID1 job, no old process birth, and an empty or removed cgroup subtree
complete stopping. `cgroup.events` supplies recursive population evidence. Missing
evidence while the group exists fails closed. The public process observer remains
active-only. One authorized stop call is issued; timeout never renews authority.

Start now waits for asynchronous collection of the fixed transient unit name
before writing a new launch journal or spawning. This includes a failed target's
durable launch intent with no observed process, so fallback can wait for collection
without inventing an identity. Surviving root/cgroup mismatches refuse, and only
stable absent/no-job observations allow a new unit. The diagnostic health path
receives a private 1 MiB tmpfs owned by the unprivileged Player, with no host write
bind or broader runtime-directory permission.

The separate [process-repair snapshot](player-node-handoff-support/resumption-process-source-inventory.json)
contains 765 non-doc paths, SHA256
`9529dc42dfe8892706d32675b1edb6071f652786800d8900668566a3b4590c31`.
Only `appliance/node/process_linux.py` and `tests/test_node_linux_adapters.py`
changed since the directory snapshot. Linux's focused set passed **60 tests in
2.10 seconds** with Ruff clean. The full portable suite completed with exit 0: **2,980 passed, 1,270 skipped,
23 warnings in 198.03 seconds**. Skip categories remain the same as the first
portable freeze. DB-backed adapter, artifact, boot, online-broker and Central
lifecycle checks passed **79 tests, one library warning in 21.27 seconds**, with
no skips. Repository-wide Ruff, 137-document link validation and the tracked working-tree `git diff --check`
passed. The later whole-checkpoint whitespace limitations are recorded below. All 765 non-doc entries still matched the process inventory after
both test processes terminated. The unchanged DB domains were not subjected to
another broad DB run; their completed broad snapshot remains explicit above.
Fresh components and actual-fixture outcomes remain pending. Earlier broad DB
results retain their original snapshot rather than being attributed to later code.

## Current-proof reconciliation and durable harness

The process-repaired actual online attempt produced ordered stop/start/running
facts and a new exact target process, app epoch and Registry authority, but one
run timed out with its drain held. The scheduler was healthy and already invoked
the real lifecycle owner. Authenticated, read-only fixture diagnostics then
isolated the missed predicate: the accepted link's control receipt lagged the
latest applied delivery despite identical content. Control polls occur every
0.5 seconds, proof retries every five seconds and scheduler ticks every second;
a valid proof can be superseded before the next scheduler cut. The instrumented
follow-up **naturally passed** in **240.95 seconds** (one passed, two deselected,
four warnings), with current receipt equality and real operational discharge.
This established timing-dependent delay, not impossible completion. Neither run
used forced reconciliation, manual ACK, fabricated process evidence or drain reset.

The reviewed repair injects a narrow current-link observer into production
composition. Link admission takes Coordination then Runtime before existing
Fleet/session/Player locks, verifies the exact current receipt and signature,
and stores the link. Within that same transaction, the lifecycle owner examines
only that device/generation's active operational attempts using its unchanged
process, authority, ordered-effect, exact-receipt and unbound predicates. The
scheduler remains a recovery path. Exact link replay invokes observation only
after fresh current-proof validation; stale replay stays idempotent and does not
grant current authority. No-effect recovery still requires its separate grant and
proof, and no CI/reboot/D17 approval boundary changed.

Twelve new deterministic PostgreSQL tests demonstrate the missed scheduler window,
atomic valid ingress, replay freshness, wrong process/authority, bound target,
missing/reversed stop evidence, invalid signature and lock ordering. Together with
19 existing lifecycle/runtime tests they passed **31 tests, one warning in
29.36 seconds**. The initial new helper incorrectly constructed a terminal event
before attaching its required process; that test-only mistake produced **12 failed,
19 passed in 27.07 seconds**, was corrected, and is not hidden as a product failure.

The standalone fixture and real PID1 launcher now have durable repository source;
its authenticated diagnostics expose only predicate booleans and counters. A smoke
check of the temporary companion passed **one test in 1.97 seconds**, covering real
HTTP, isolated DB, authentication and exact archive bytes, without fabricating
process evidence. The final composed scenarios use the durable entry point.

The [integrated source inventory](player-node-handoff-support/resumption-integrated-source-inventory.json)
contains **770 non-doc paths**, SHA256
`03cfb058c38ac36330473e5a53a484d27b5a150466cdeaa8309b3422301adde1`.
It includes the three Central production changes, new observer tests and four
harness sources. The full portable suite passed with exit 0: **2,980 passed, 1,282 skipped,
23 warnings in 242.45 seconds**. Its twelve additional skips are the new observer
checks without PostgreSQL configuration: 937 DB skips total, with the other
categories unchanged. All 770 non-doc entries still matched this inventory when
the portable process terminated. The supplemental full DB suite completed with exit 0: **3,913 passed,
348 skipped, one expected failure, 24 warnings in 1,038.85 seconds (17:18)**.
There were no missing-DB skips; the skip categories and documented legacy media
flock expected failure remain those recorded above. These new Central changes
justified another complete DB run. Repository-wide Ruff, 139-document link
validation and the tracked working-tree `git diff --check` passed at the broad-suite freeze; that diff did not cover then-untracked files.

During the DB run, separately excluded standalone harness files were repaired to
assert final actual process identity for no-effect recovery and to guarantee
owned-container cleanup even when diagnostic collection fails. All production
and broad-collected test bytes still matched the integrated snapshot at DB
completion. The separate [later harness inventory](player-node-handoff-support/resumption-harness-source-inventory.json)
contains **773 non-doc paths**, SHA256
`0e63eb15dc1770988859af74e7ebb50a76c110a0978d243950be7362a11473e3`.
Only six `tests/node_pid1_*` harness paths changed or were added. The prior broad
suites are not retroactively attributed to these later harness bytes. Its owner
reported explicit collection of three scenarios and clean Ruff/docs; actual
scenario execution remains separately reported by the Linux owner. The final
Linux run `99225` ended with exit 2 after **434.48 seconds**: its success case
failed with `stop_outcome_unknown`, the following failure case was interrupted,
and no-effect was not run. All owned `photo-wall-resume-*` containers were confirmed
absent. The strengthened harness and diagnostic wrapper have not yet been
executed or independently re-reviewed; collection is not qualification. The final
Ruff check and 140-document link check passed, and all 773 source entries matched
the saved later harness inventory after the Linux owner declared its freeze.

Subsequent user steering requires the stop adapter to own a Promise/Future
operation and bounded observation/recovery, including scoped reboot recovery
when Player control cannot be restored. This explicitly supersedes indefinite
unknown/held-device behavior as the intended recovery policy. The
[StopOperation proposal](2026-09-30-node-stop-observation-proposal.md) is the next
design checkpoint, not implemented behavior. Its architecture and implementation
are not covered by the completed snapshot above; future repairs require their
own source identity and validation. Physical reboot/deployment and unrelated
CI/D17/transport-renewal approval boundaries remain separate.

The earlier `components-stopfix` inventory remains the packaged node identity.
Independent reopening of its source provenance confirmed all **49 Player and
23 manager Python files**, plus each archive's four recorded build inputs, still
match the current checkout. The later Central/harness snapshot is not relabeled
as the source identity of those already sealed artifacts.

## Full-base tooling preparation

The separate [full-base tool checkpoint and sanitized recipes](player-node-handoff-support/full-base/README.md)
record an immutable arm64 builder, exact package inventory and a successful
isolated mount prerequisite probe. That initial checkpoint was preparation only, with no full image or boot claim. The initial
Perl package-skew failure and subsequent snapshot-normalized build are retained
in the checkpoint's original temporary log locations. Shared CI permissions and
physical qualification remain separate from the standalone tool probe.

The later [full-base build record](2026-09-30-node-full-base-build.md) closes the
production rootfs/kernel/initrd/base and V2 PXE bundle artifact build for the
coherent stopfix node closure. Exact content checks and a restricted real stage-1
initrd mount probe passed. Executed sanitized recipes and compact verification
outputs are preserved separately from earlier prepared texts. Full-squashfs PID1
composition and physical Pi/PXE boot remain distinct qualification steps; a
constructed bundle is not release certification. The Linux owner also verified a
container image derived from the full squashfs against all 30,554 baseline
filesystem entries using the saved layer, with exact equality. That image-content
verification is distinct from executing its repaired lifecycle scenarios.

## Remaining full scope and approval boundaries

The [handoff's pending decisions](2026-09-30-player-node-handoff.md#decisions-and-explicit-approvals-still-pending)
remain in force. Continuation did not authorize CI revocation/gate/schema changes,
reboot carrier renewal, D17 guard/admission/audit/signer/publisher/IaC changes,
persistent privileged CI, or automatic root cache reclamation. D16 active-Run and
due-Program behavior still require selection; bound planned withdrawal refuses.

The finite remaining work includes final actual repaired-unit cold/online
stop/start/fallback/no-effect composition with the repaired Central owner, exact
full-squashfs process composition, the newly directed StopOperation/Future and
bounded reboot recovery implementation, then exact-image serving/rollback and
distinct-authority qualification. The integrated portable/full DB checks are
complete for the pre-Future implementation snapshot. The complete
production base/PXE artifact build and restricted initrd mount probe now have
separate completed evidence. Full base
PXE boot, physical dual-Output DRM/HDMI continuity/calibration and the combined
8 GiB pressure/network envelope remain unqualified. Headless rendering, database
checks and fixture signatures do not substitute for those outcomes. The
[implementation map](../player-fleet-implementation-map.md) retains the complete
scope and the [Linux record](2026-09-30-node-linux-integration.md) owns its next
artifact checkpoint.


## Next-session checkpoint

| Scope | Saved identity | Completed checks / limits |
|---|---|---|
| Broad integrated repository | 770 paths, `03cfb058c38ac36330473e5a53a484d27b5a150466cdeaa8309b3422301adde1` | Portable 2,980 passed / 1,282 skipped; supplemental DB 3,913 passed / 348 skipped / one documented legacy expected failure. No missing-DB skips in the supplemental run. |
| Later standalone harness | 773 paths, `0e63eb15dc1770988859af74e7ebb50a76c110a0978d243950be7362a11473e3` | Six excluded harness files changed/added after broad collection. Production and broadly collected tests unchanged. Explicit three-scenario collection and Ruff passed; latest strengthened harness execution and independent re-review remain pending. |
| Packaged node and full bundle | `components-stopfix` source manifest `793834f315daafaee704a5e59fc2a29748417615a02a500cd1bf5182531b9801` | Exact Player/manager closures still match current source; full component, bundle and scoped stage-1 mount checks passed. Later Central/harness source is distinct. |
| New stop/recovery direction | Proposal only; no implementation claim | Adapter owns the Promise/Future, transient observation and durable reattachment. Bounded recovery can request reboot through the existing base/Host owner when Player control cannot be restored. Physical execution remains separate. |

Resume in this order:

1. Revalidate the latest non-doc inventory and artifact closure before editing; preserve prior broad and packaged-node identities.
2. Finalize and implement the reviewed adapter-owned stop operation and bounded base/Host recovery. Service local progress independently of network availability, keep one BootStore writer, distinguish pending observation from guarantee-unavailable, and preserve exact permit/effect/restart ownership. Do not strand the device indefinitely on a transient Player fault.
3. Verify the new boundary with deterministic process/cgroup/network/restart tests, then rerun the required scoped/broad checks justified by the changed owners. Complete the durable actual cold/target/fallback/no-effect scenarios against the final implementation, with real process and cleanup assertions.
4. Complete exact full-squashfs process integration, serving/rollback certification and physical/PXE/display/pressure qualification; retain the full scope and all separate approval decisions above.

No additional broad tests are needed merely for this documentation checkpoint.
The literal `test_local.py` missing-`.env` limitation remains; do not replace it
with a claim that the exact command succeeded. Final evidence is reviewable
working-tree material; Git/PR publication is a separate orchestrated handoff.


## Publication whitespace-check correction

After staging the complete checkpoint, `git diff --cached --check` found four
nonfunctional whitespace warnings that the earlier tracked working-tree
`git diff --check` did not cover:

- `appliance/node/capacity.py:53`: new blank line at EOF.
- `appliance/node/clock.py:15`: new blank line at EOF.
- `appliance/node/http.py:33`: new blank line at EOF.
- `docs/evidence/2026-09-30-node-reboot-carrier-proposal.md:9`: whitespace-only line.

These files were previously untracked when the narrower working-tree check ran.
The whole-checkpoint whitespace check therefore did **not** pass cleanly. The
source and document bytes are preserved to retain the tested inventory and
artifact identities; no functional repair or test rerun is claimed for this
reporting correction. Ruff, documentation-link and test results retain their
separately recorded scope.
