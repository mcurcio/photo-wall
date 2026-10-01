# Player node implementation handoff — 2026-09-30

**Later continuation:** [resumption evidence](2026-09-30-player-node-resumption.md) records the fresh-session validation and subsequent repairs. The saved checkpoint and inventories below remain historical.

**Paused at the user's requested stable handoff point; the full objective is not
complete.** Start here and in the [implementation map](../player-fleet-implementation-map.md),
then consult the [node domain model](../player-node-domain-model.md) and owner
records below. Earlier milestone text is historical; use the latest dated
checkpoint for current status. No commit, deployment, production command, physical
reboot, or hardware qualification was performed for this work.

## Checkout and working method

- Exact checkout: `/Users/matt/.codex/worktrees/readiness-design/photo-wall`.
- Branch: `codex/readiness-design`.
- Git HEAD: `0a9b530caae00757ebae66b74b47adb38f1a5fb0`. This identifies the parent
  commit, **not** the extensive shared uncommitted implementation.
- Implementation inventory and final stable process state are recorded in the
  checkpoint below. Preserve all dirty and untracked work; do not reset this tree
  or substitute the separate `95cc/photo-wall` checkout.
- The next top-level agent remains an **orchestrator only**: design module boundaries,
  delegate bounded implementation leaves, coordinate shared contracts and ownership,
  and integrate reviews/evidence. Follow AGENTS and CONTRIBUTING. The original full
  node objective remains in scope; do not substitute a headless demo or API-only
  calibration flow for it.
- Approval for continuing the goal, or resolving an approval-service usage limit,
  does not authorize separately rejected security changes. Preserve the pending
  decisions below. Do not bypass a rejected write through another module or tool.

## Current implementation and evidence

| Owner area | Implemented and verified scope | Practical limit |
|---|---|---|
| Central node | Durable sessions/producer evidence, boot ambiguity CAS, cold offer/release catalog, unbound lifecycle, exact process linkage and Output loss/recovery, mounted node/operator routes | Bound planned withdrawal remains refused; default-closed rollout does not certify deployment |
| Central upgrade repair | Migration060 recovers a unique historical Frame for unresolved losses or aborts atomically; five PostgreSQL regressions passed | No migration against production data was performed |
| Display and Player | Base-owned Weston14 shell/private diagnostic, app-private frame-v3 bridge, real GDK render commit/presentation, same-process revision promotion, exact Frame withdrawal, CalibrationTrial/Save/expiry restoration and operator UI | Headless compositor evidence establishes contribution to a frame, not physical pixels or dual-Output timing |
| Display composed package | Frozen real Player/bridge scenario5882 passed in22.46s: candidate/handoff, Trial/edit/partition restoration/restart/Save, actual media witness, immediate unbind after revision promotion, native removal before response and durable completion | PID1 identity and Plan/Commit authorization are explicit fixtures; no NodeAcceptance serving-image certification |
| Withdrawal regression | Seven DB tests passed in6.94s. Prior immutable revision/handoff plus fresh original receipt identifies only an old role for current Runtime-authorized removal | It cannot grant recovery, admission, or a replacement role; native/Player bytes unchanged |
| Operator browser | Calibration walkthrough and node walkthrough27714 passed; latter89.06s includes stale boot CAS, lost reboot response and exact same-command retry | Synthetic fixtures, no reboot consumer; command receipt/initiation/later boot/physical completion stay separate |
| Linux and packaging | Real arm64 Debian closure and sealed archive/member/source/native verification; subsequent base source repairs are summarized below | Pre-repair archives are milestones and must be rebuilt/resealed before final claims |

Detailed evidence: [Central](2026-09-30-node-central-integration.md),
[Linux](2026-09-30-node-linux-integration.md),
[display/native/GTK/browser](2026-09-30-display-host-native.md).
The latest display slice passed scoped Ruff and the documentation checker
(127 Markdown documents at that point). Full repository pytest/Ruff/local DB
verification was **not run** after final source changes: the user requested this
pause before broad execution. Earlier leaf and milestone suites cannot stand in
for that final pass.

## Frozen artifacts versus current source

Verified pre-base-repair snapshot: `/private/tmp/photo-wall-node-frame3-review`.
Its `source-inputs.json`, `runtime-inputs.json`, `build-provenance.json` and
`artifact-verification.json` describe exact bytes; do not infer identity from a
mutable Docker tag. The [Linux record](2026-09-30-node-linux-integration.md#coherent-frame-v3-artifact-snapshot)
contains full app/manager archive, dependency lock and source hashes.

- Native artifacts: `/tmp/photo-wall-native-frame3-build`.
- Native `.deb` SHA256: `00731c94838f064a147605fac011739f995d3771f00d7ea783373131fc613ebe`.
- Private bridge SHA256: `a76f6ae10ffecc7c61242161a22106539056c2337fbf8c5c12ed551ab29120e6`.
- Graphics ABI: `weston14-3734f476404779775c755b829a6583bcfeb9142a52f3c30a6b15b97709e3f31f`, frame-v3.
- Frozen Player dependency image: `sha256:461acad6ed3291a485ed7289d3136e5126ec70bd7f85de6c2a372e09a378bf4e`.
- Passing GTK fixture image: `sha256:7bc8b063f6bb22cd05036f469c8760243e1635c15cc721b6c9a6c967c517b7d1`;
  includes declared native libjansson4 dependency. This differs from the Linux
  matching-artifact fixture image590ceb described in its record.

The subsequent capability, IPC directory, XDG/PAM/seat, HostCore telemetry/outbox,
and mountpoint changes alter base/sealer identity. They require a coherent new
base ABI and app/manager environment references. Existing native/Player bytes can
be reused only after confirming their exact source closure remains unchanged.
A compact fixture reseal is not a rebuilt final release. Temporary artifacts/logs
may disappear; durable evidence records preserve claims and identities, not binaries.

## Decisions and explicit approvals still pending

1. **D16 active Run:** for explicit Update now, interrupt only this Player's Outputs
   and keep other participants (review recommendation), wait idle/expire, or cancel
   the Run with Actuator safe state. No option has been selected.
2. **D16 due Program:** skip without replay (review recommendation), or late-start
   within the original window. No option has been selected. Bound online stop
   continues to refuse; recommendations are not authorization.
3. Persistent privileged PID1 probe invocation in shared CI: explicit approval
   remains unanswered. Authorized standalone fixtures do not authorize pipeline use.
4. Automatic unused-root cache reclamation: approval remains unanswered. Capacity
   exhaustion continues to refuse preparation; no destructive cleanup was applied.
5. CI revocation/gate/schema control-boundary repair: automatic review rejected the
   write before execution for insufficient explicit authority. The proposed gate
   closure, durable revoked-scope veto and higher-generation revoked outbox are
   **unapplied**. Existing active evidence may otherwise admit new effects until its
   lease expires; there is no selected grace policy.
6. Reboot response carrier renewal: separate automatic review rejected the proposed
   authentication-boundary change before execution. Original response receipts still
   require the issuing session; the proposal preserves original command identity
   while allowing exact same-producer renewed transport. It is **unapplied**.
7. D17 guard/gate mutation authority, authenticated admission/audit ingress, signer and
   narrowly scoped Kubernetes publisher permissions require explicit approval.
   Migration062, guard service/domain, live publisher and IaC wiring are absent.
   Usage-limit recovery did not approve these changes.

The clean IaC checkout is `/Users/matt/.codex/worktrees/2dff/iac`; issue `iac-g9ut`
was created/claimed only. No IaC guard edits, sync, push, preview, apply or deployment
occurred. Preserve workload/platform boundaries and applicable IaC skills.

## Finite remaining work and resumption order

1. Read the final Linux checkpoint and durable support documents below. Resolve
   explicit approvals/product questions without treating continuation as consent.
2. Complete any narrowly named remaining Linux fixture defect, then freeze inputs,
   rebuild the coherent base/Player/manager references and verify archive/source/ABI
   membership. Exercise actual repaired-unit cold and online stop/start/fallback/
   no-effect paths against Central; don't replace PID1 proof with fake callbacks.
3. With approval, implement CI revocation transaction and reboot carrier repair,
   their concurrency/renewal regressions, then the designed D17 guard ledger,
   authenticated admission/audit, immutable signed holds and exact publication/IaC
   composition. Preserve issued hold deadlines and unresolved rollback barriers.
4. Resolve D16 before implementing bound planned withdrawal semantics. Keep unbound,
   cold failure recovery and independent reboot behavior distinct.
5. At a stable source point run required broad checks, review migration upgrades,
   and update the implementation map/evidence. Run actual exact-image qualification
   with configured distinct signing authorities; fixture signatures are not release
   certification. Privileged CI inclusion needs its own approval.
6. Qualify supported actual PXE boot, serving/rollback topology and enforcement,
   physical DRM/HDMI dual-Output handoff/continuity/calibration, and combined8GiB
   pressure/network envelope. No headless or database result fills those claims.

## Resume commands and prerequisites

From the exact checkout, after coordinating owners and source freeze:

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
python3 scripts/check_docs.py
.venv/bin/python scripts/test_local.py -q
```

The checkout currently has no `.env`, so `scripts/test_local.py` cannot run as-is.
Do not create persistent credentials for a test. The already-authorized local
Compose fallback reads `photo-wall-database-1` credentials in memory and runs
pytest in disposable random schemas. Its durable helper is linked below; no
credentials are printed or retained. Report the exact required command limitation
separately from supplemental database coverage. Native/browser/PID1 opt-in checks
remain separate from broad-suite skips.

To repeat the frozen GTK scenario only after a relevant source/artifact change:

```sh
PHOTO_WALL_NATIVE_DISPLAY_IMAGE=sha256:7bc8b063f6bb22cd05036f469c8760243e1635c15cc721b6c9a6c967c517b7d1 \
PHOTO_WALL_DISPLAY_ARTIFACT_DIR=/tmp/photo-wall-native-frame3-build \
PHOTO_WALL_FROZEN_PLAYER=/private/tmp/photo-wall-node-frame3-review/player/usr/lib/photo-wall-player \
PHOTO_WALL_GTK_PROBE=1 PHOTO_WALL_MEDIA_PROBE=1 \
PHOTO_WALL_NATIVE_EVIDENCE_LOG=/tmp/pw-frame3-frozen-render-evidence.log \
.venv/bin/python docs/evidence/player-node-handoff-support/run-isolated-db-tests.txt tests/test_node_display_native.py -s
```

No temp-path existence, container identity or live process should be assumed in a
new session. Revalidate first; do not restart an existing long build on timeout.


## Final stable checkpoint and durable support

The implementation inventory covers **764 nonignored paths outside
`docs/`**, including tracked and untracked source/config/test inputs and symlink
targets. Its canonical entry-list SHA256 is **`7bee1a97c254e761fceffb0fdabf7f7f298f8aae01b3e93f43ad271439ada926`**.
The [inventory JSON](player-node-handoff-support/implementation-source-inventory.json)
records every path/hash/mode and exact scope. Documentation is excluded to avoid
self-reference while saving this handoff; this is a dirty-source snapshot, not a
commit or artifact digest. The [Linux35-file manifest](player-node-handoff-support/linux-source-sha256.json)
has file SHA256 `5a75b5f4bd21ef44c622e21c970e0f41e3876b9bfcbe56a00ad28f76e6729806`.

Linux's [final stable checkpoint](2026-09-30-node-linux-integration.md#stable-pause-checkpoint-after-linux-unit-repairs)
supersedes its earlier audit. All six repairs are applied, including protected
socket crash-state recovery, fixed post-PAM XDG, and the empty manifest-declared
config bind target. Compact reseal74245 passed; actual restricted-unit6335 passed
with unchanged retained client reconnecting, measured exact capabilities, private
runtime permissions, post-exit root inventory unchanged, and PAM/private pseudoTTY
checks. Focused83877 passed **71 tests in2.06s**. Physical tty1/DRM remains unqualified,
and explicit PAM/logind dependency coverage must be checked in the production
base (the fixture inherits those packages from a Player closure).

The compact root archive is
`fc692511d4435a62ebbf25514d3d13434df2364b15f049b008e77de28eecca3d`;
fixture image is `sha256:4ed7bc287bc3c7ae9a13356e4bd2ae0209b87e688c4e5ac9da4efec14e7187b2`.
Neither is a complete new coherent release set. No broad suite was started after
the pause request. Documentation/whitespace checks are the only final handoff checks.

**No live owned build/test/probe handles remain.** Central and Display handles
are terminal; Linux confirmed its disposable `photo-wall-six-repair-pid1` container
absent. Existing local development infrastructure was not stopped. A direct host
process listing was sandbox-refused, so the no-owned-process statement rests on
terminal tool results and each owner's cleanup confirmation, not a global host
process audit.

Durable review and resumption material:

- [Central authoritative audit and historical trail](2026-09-30-node-central-handoff-audit.md).
- [Unapplied CI revocation design](2026-09-30-node-ci-revocation-proposal.md).
- [Unapplied reboot carrier proposal](2026-09-30-node-reboot-carrier-proposal.md).
- [Unapplied D17 guard/API/ledger/IaC specification](2026-09-30-node-guard-proposal.md).
- [Historical Linux audit](player-node-handoff-support/linux-pre-repair-audit.md) and
  [pre-repair artifact checkpoint](player-node-handoff-support/linux-pre-repair-artifact-checkpoint.md);
  both are visibly marked superseded for current source status.
- [Isolated local DB harness](player-node-handoff-support/run-isolated-db-tests.txt),
  invoked with `.venv/bin/python` and test arguments.
- [Standalone PID1 outer fixture](player-node-handoff-support/run-node-ipc-probe.txt),
  preserved as text for review. Its exact temporary image/reference paths must
  exist and be verified before an authorized standalone rerun. This is not CI
  authorization; do not execute merely because it appears in this handoff.

The repository owns the resumable designs and inner fixture source; the next
session need not recover this conversation or temporary audit files.
