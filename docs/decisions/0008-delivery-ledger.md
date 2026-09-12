# 0008 delivery ledger

Implementation of the accepted design [0008](0008-generic-image-and-serial-identity.md).
Scope of this push (owner-chosen 2026-09-11): **software baseline + D0 flash `.img` + hardware serial**.
Deferred: T1/T2 transport trust, I1 certificate tier, D1 netboot `Release` re-freeze, GHA publish.

Land loop per `~/.claude/skills/implementation-workflow/SKILL.md`: implement → verify (full PG gate) → review (high-risk only) → land. One bead at a time in this checkout. Budget: 90 min / 8 agents per bead.

## Milestones & beads

| Bead | Milestone | Package(s) | Risk | Status | SHA |
|---|---|---|---|---|---|
| (design record) | — | docs | — | closed | c976e48 |
| m1-central-lifecycle | M1 central lifecycle (TRACER) | central | authz — reviewed | closed | 4016e43 |
| m2-player-discovery | M2 discovery | player | authz/transport — reviewed | closed | 5c0de40 |
| m2-mdns-browse | M2 discovery | player (+dep) | medium | closed | a8094ab |
| m2-mdns-advertise | M2 discovery | central | low | closed | 58f2448 |
| m3-hardware-serial | M3 flash image | player | medium | closed | ee0396d |
| m3-central-d0-enroll | M3 flash image (tracer completion) | contracts+player+central | authz trust-boundary — reviewed | closed | ad876b5 |
| m3-flash-image | M3 flash image | appliance | infra — CI/hardware gate | landed (CI pending) | 5241f6e |
| baseline-docs | all | docs | low | closed | 6374db8 |
| ci-fixes-1 | integration | player+central+tests | boot-path | closed | a12a79d |

Status values: open · in_progress · blocked (wip branch) · closed.

Docs consolidated into one `baseline-docs` bead (0008 step 7: single flash-and-go
rewrite of runbook / module-pxe-service / README). Code beads never block on it,
but it MUST land before the baseline PR merges (implementation-workflow §3.7).

**baseline-docs must-cover items (found during delivery):**
- Port coupling: central advertises `PHOTO_WALL_HTTP_PORT` (default 8000). A deployment
  that changes the served port (Dockerfile `--port` / compose `PHOTO_WALL_PORT`) MUST
  also set `PHOTO_WALL_HTTP_PORT` or players discover a dead port. (verifier finding, m2-mdns-advertise)
- `PHOTO_WALL_MDNS_ADVERTISE=false` disables advertising (for fully-explicit-config deployments).
- `docs/module-player-service.md:13,:33` claims boot-context `persistence` is "always volatile" —
  now false for flashed D0 players (`persistent`). Update it. (m3-hardware-serial review finding)

## Log
- **M1 (tracer) landed 2026-09-11.** `Registry.unbind` + `DELETE .../binding` +
  `PlayerInventory.is_bound` pending view. Full PG gate green (1493 passed);
  adversarial authz review clean; verifier confirmed 3 mutation probes bite +
  a session-survives-unbind regression test. Errata: 2 entries (epoch-vs-generation
  wording; `is_bound` defaulted non-required) — both benign, see `.claude/errata.md`.
- **First CI run on PR #3 red on all three workflows; fixed in `a12a79d`.** (1) appliance
  `prepare_image` smoke-test pulled `zeroconf` via a module-top import → made lazy; (2) mDNS
  unit tests discovered the real compose central on CI's Docker bridge → unique per-test
  service types; (3) e2e startup timed out because central awaited mDNS advertising before
  readiness → moved to a background task. All local gates green (1538 passed); re-running CI.

- **CI cycle 2 (`9eea67a`), root-caused from artifact data (no guessing).** Second CI run:
  e2e went green (background-advertise fix); MVP had 1 residual flake; appliance still red at
  `prepare_image`. Downloaded the failed run's `pip-install.log` diagnostics artifact →
  proved the appliance wheelhouse omitted `zeroconf`/`ifaddr` (16 wheels, neither present):
  `scripts/build_player.py` `ROOTS` allowlist was never updated for the new dep, so
  `locked_runtime` silently dropped it. Fixed ROOTS. Made `appliance/build.py` `run()` emit
  the failing tool's output tail (was swallowed) so future build failures are diagnosable in
  the GHA log. Also made `test_late_discovery` deterministic (reproduced the shutdown race
  offline; 30/30 green).

- **CI cycle 3 (`9eea67a`→fix): MVP green, appliance root-caused via the new diagnostic.**
  MVP checks passed (late-discovery + mDNS isolation fixed). The `run()` diagnostic printed the
  real appliance error: `appliance/bootstrap.py` imports `contracts.equipment`, but the rootfs
  copy allowlist (`build.py:763`) and the source inventory (`:440`) didn't include it → added
  `equipment` to both. Same allowlist-not-updated class as the ROOTS bug. (e2e that cycle failed
  on an unrelated infra hiccup — Immich fixture container `docker_command_failed` — retrigger.)

- **CI fully GREEN on `38f0c50` (2026-09-12): MVP checks, e2e, AND the ARM appliance build+QEMU boot+Player enrollment all pass.** Baseline verified end-to-end in CI (not just locally). Boot fix (`contracts.equipment` in the initramfs) confirmed; OS base restored in ~1 min after the base_key parity fix; P2 (candidate reuse) + P3 (KVM probe/TCG fallback) landed without regression.

## Build-time perf (owner raised)
- OS-base "Pull" 16 min → ~1 min: `base_key` no longer tracks `run()`'s diagnostic AST (`38f0c50`). P2 candidate-tag reuse is insurance for genuine base changes; P3 uses KVM when a runner exposes `/dev/kvm` (inert TCG fallback on hosted arm64).
- Assemble ~8 min: investigated. Safe next win = R1 (mksquashfs `-processors` 2→nproc, ~60-120s, needs a byte-identical `rootfs_sha256` check). Owner-decision items: R2 (skip rollback candidate in smoke scope, ~157s) and R3 (overlap rollback squash with finalize VM work). Non-starters: `--*-cache` hooks (wrong code path), changing compressor/block size (alters signed bytes).
- Framework question (Yocto/rpi-image-gen/pi-gen): recent pain was incidental (duplicated allowlists + leaky cache key), not a framework failure; a swap likely isn't faster and would re-implement the signed-release/netboot model. If pursued, scope it to the D0 flash tier only, as a deliberate design decision.

## Phase 2 — PXE + published image (0008 steps 5-6; owner reprioritized 2026-09-12)
Owner's fleet is DISKLESS (netboot). PXE and a published image are deliverables, not deferred.
Signing decision: **CI signs with a persistent release key stored as a GitHub Actions secret.**
Directive: build as much as possible up to the software/QEMU line; owner hardware-tests later.

Slices (tracer-first):
| Bead | Package(s) | Risk | Status |
|---|---|---|---|
| p2-d1-refreeze | contracts+appliance+central | signature/migration — reviewed | closed `c32ad7a` |
| p2-netboot-config-relocation | appliance/boot | boot-critical — OPEN DECISION | open (needed only for generic netboot rootfs) |
| p2-signing-key | appliance/build+workflow | security (key) — reviewed | closed `6bf2fe7` |
| p2-release-workflow | .github/workflows | infra | open |
| p2-flash-in-ci | .github/workflows+appliance | infra (unverified boot) | open |
| p2-operator-tftp | scripts+docs | medium | open |
| p2-docs-pxe-first | docs | low | open |

**Owner hand-off actions (only you can do these):**
1. Generate the release Ed25519 keypair; commit `release.pub.pem` (trust anchor); add the private key as the GitHub Actions secret the release workflow references. I build the plumbing around a named secret and never handle the private key.
2. Real Raspberry Pi 5 PXE boot qualification (DHCP next-server/filename, TFTP, EEPROM netboot) — CI cannot exercise the PXE transport; it only boots RAM-root under QEMU.

## Phase 3 — 0009 re-architecture (minimal base OS + app-as-.deb-from-central; accepted 2026-09-12)
Owner ruling: home LAN, no threat model, UX over security. Supersedes 0008's D1 signed-RAM-root
tier. Keeps serial identity, mDNS, pending/bind/unbind. Retires the release-authority/boot-ticket
machinery and the bespoke signing. **Supersedes** the earlier Phase-2 slices `p2-signing-key`
(`6bf2fe7`) and the signed-rootfs publish in `p2-release-workflow` (`59616d1`) — reworked/retired
by slices 3 & 6 below. The five-field Release re-freeze (`c32ad7a`) is largely subsumed by slice 3.

Slices (tracer-first; design: [0009](0009-minimal-base-and-app-package.md)):
| Slice | Package(s) | Delivers | Risk | CI? | Status |
|---|---|---|---|---|---|
| **T (tracer)** p3-enroll-rekey | player+central | diskless ticketless enroll re-keyed off "no boot ticket present"; registry.py:92 guard move | HIGH (fleet-brick) | yes | closed `8f35e5c` (verified+reviewed) |
| 1 p3-central-app-service | central | app manifest + package endpoints + current-app pointer | med | yes | closed `101a140` |
| 2 p3-base-bootstrapper | appliance | discover→fetch→verify sha256→unpack→run→origin handoff | HIGH boot-critical | mocked | in_progress |
| 4 p3-deb-build | scripts | player .deb (prebuilt venv) | med | yes | closed `beeaf9f` |
| 5 p3-base-image | appliance | minimal generic base OS image (NEW path, additive) | med-high | structure only; real Pi = owner | in_progress |
| 6 p3-release-workflow-rework | workflows | publish base image + .deb (unsigned) | med | yes | closed `d107798` |
| 7 p3-docs | docs | README/runbook/module docs to the new model (honest, mid-migration) | low | link-check | closed `45e8703` |
| 3 p3-retire-authority (RESEQUENCED LAST) | central+contracts+appliance | delete old netboot/release-authority/boot-ticket/rootfs/trial + signing | HIGH destructive | yes | **deferred until owner hardware-validates the new base-image boot** |
| 6 p3-release-workflow-rework | workflows | publish base image + .deb; retire signed-rootfs publish | med | yes | open |
| 7 p3-docs | docs | README/runbook/module docs to the new model | low | link-check | open |

Owner hardware hand-off: real Pi 5 netboot of the base image (slice 5) + full tracer on hardware.

## Residuals (follow-up, not blocking)
- **Single source of truth for bootstrap's contracts/appliance module set** — currently duplicated across 4 sites (wheelhouse `ROOTS`, dist-packages copy, initramfs hook, `verify_initramfs`). Collapsing this kills the failure class that caused three CI cycles here.
- Health-JSON `persistence` (`player/service.py` `_write_health`) is hardcoded `"volatile"`
  even for a D0/persistent player — telemetry-only inaccuracy; docs describe actual behavior.

## Errata
Append-only spec contradictions found during implementation live in `.claude/errata.md`.
