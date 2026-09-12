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

## Phase 4 — rpi-image-gen base + dependency-via-.deb (owner-approved 2026-09-12)
Owner rulings: (1) SPIKE rpi-image-gen NOW for the base OS (delete our custom guestfs/squashfs/
os-base pipeline, ~2,500 LOC, once proven); (2) the `.deb` DECLARES its full runtime deps
(GTK/GStreamer/weston/Mesa/…) and the bootstrapper installs it **via apt** so those deps are
pulled from the distro repo at boot — base stays minimal (OS + apt + sources + bootstrapper +
python/zeroconf). (3) Retirement of the old signed netboot/release-authority is GREEN-LIT.
pi-gen rejected (SD-only, no netboot). rpi-image-gen: Debian re-base, emits a squashfs via
genimage; we still own a slim netboot init + TFTP assembly. Only runs in CI (arm64/podman).

Approach: spike-first (prove rpi-image-gen builds our base in CI) → adjust slice 4 (.deb full
Depends) + slice 2 (apt-install) → wire the netboot boot chain on the rpi-image-gen base → retire
the custom pipeline + old signed path. 0009 gets updated once the spike proves viable.

| Slice | Delivers | CI? | Status |
|---|---|---|---|
| p4-rpi-image-gen-spike | minimal-base rpi-image-gen config + CI build job (emits base squashfs) | CI build (arm64) | **PROVEN** — builds + content-verified green (`d526428`); folded into PR |
| p4-package-all-custom | ALL custom code shipped as portable .debs: bootstrapper .deb (in base) + player .deb (at boot); base install = apt-install our .debs, no tool-specific overlay | yes | **PROVEN** — rpi-image-gen base builds + apt-installs the bootstrapper .deb + content-verified green |
| p4-boot-chain **s1** (slim netboot init) | appliance/netboot_init.py: cmdline base_url -> HTTP fetch -> corruption sha256 -> reuse LinuxOps.mount_root -> pivot; writes NO ticketed context (app enrolls ticketless). Additive; old bootstrap.boot() untouched | unit (14 tests) | **closed `653f4bf`** (verified+probed) |
| p4-boot-chain **s2a** (netboot-initrd builder) | portable initramfs-tools hook + boot script running `python3 -I -m appliance.netboot_init` + standalone content-verify (staged closure: netboot_init + appliance/{__init__,bootstrap,provision} + contracts/{__init__,release,equipment} + python + /scripts/functions; NEGATIVE: no updates.py/signed material/zeroconf/gtk/central). Reuses the build.py pattern, NOT build.py itself (retiring) | unit (verify fn) | **closed `47d5ed9`** (verifier PASS, 31 probes) |
| p4-boot-chain **s2b** (bundle in CI) | base-image.yml builds a scratch Debian-trixie-arm64 root (linux-image-rpi-2712 + raspi-firmware + initramfs-tools + python3; RPi archive `trusted=yes` — SHA1 InRelease rejected by sqv), installs the s2a hook+script, `mkinitramfs`, assembles bundle {config.txt, cmdline.txt template, kernel_2712.img, initrd.img, bcm2712-rpi-5-b.dtb, overlays/, photo-wall-base.squashfs, SHA256SUMS}, content-verifies (no hardware) + uploads | CI build (arm64) | **closed `2d4be4a`** (CI green run 34713276980; kernel 6.18.39+rpt-rpi-2712, initrd 24.6 MB, squashfs 64.5 MB) |
| p4-boot-chain **s3** (process-level e2e tracer) | Part A (netboot-e2e.yml, self-contained): real central+Postgres serves the REAL new-model player .deb; the REAL Bootstrapper apt-installs it in a trixie container (real dep resolution) + sha256 verify + flip-a-byte refusal + **py3.13 import smoke** (player.service + gi/Gtk/Gst/OpenGL resolve on trixie). Part B (demo_wall, software-e2e): ticketless enroll -> pending -> operator bind -> render smoke. | CI (arm64, no VM) | **closed** — Part A `b225729` (run 34720913721 green), Part B `8230f0c` (run 34714823063 green). GATES retirement — now satisfied. |
| p4-deb-full-depends | player .deb apt-deps model (Python 3.13, no venv/chroot) | CI arm64 | **closed `ae12538`** (run 34717037113: build + trixie apt-simulate resolve green) |
| p4-retire | delete custom image pipeline (~2,500 LOC) + old signed netboot/release-authority/boot-ticket/trial | HIGH destructive | open (green-lit; **owner authorized FULL retirement incl. s6 DROP appliance_* tables** once s3 green, 2026-09-12; do s4 client+central code -> s5 old build pipeline -> s6 table drop LAST) |

### Retirement (owner authorized FULL retirement 2026-09-12; corrected order so the build survives)
Order: **s5 (retire old build pipeline) -> s4 (retire old signed client+central path) -> s6 (DROP appliance_* tables, LAST/irreversible).** All gated on s3 (now closed). s5 before s4 because the old appliance.yml QEMU e2e (test_appliance_e2e) exercises the signed routes; delete those workflow jobs (s5) before/with removing the routes (s4). netboot_init.py + s2a keep appliance/bootstrap.py (LinuxOps.mount_root) + contracts/release.py (MAX_ROOTFS_BYTES) — s4 strips ONLY the ticket/signature/trial parts, not those symbols.
Owner chose FULL (2026-09-12): migrate the media-OS test base off the old pipeline FIRST, then delete the whole pipeline. New order: **p4-media-os-migrate -> s5(full) -> s4 -> s6.**
| Slice | Delivers | CI? | Status |
|---|---|---|---|
| p4-media-os-migrate | replace the service_base/ci_images/os_base media-OS builder (software-e2e + checks dep) with a simple base (plain Debian/mmdebstrap or a Dockerfile of the media worker's native deps), so nothing but the retiring appliance uses appliance/build.py + os_base | software-e2e + checks green on new base | open (next) |
| p4-retire **s5** (build pipeline, FULL) | after media-os-migrate: delete appliance/build.py + os_base + os_packages + os_definition.json + fetch_ubuntu + ci_base_cache + ci_images + build_ci_*/vm_*/boot_gateway/boot_fixture/rollback + old service_base bits + appliance.yml; rewire release.yml to publish base bundle + player .deb + bootstrapper .deb | yes | open |
| p4-retire **s4** (signed client+central) | delete /v1/bootstrap/boot, /appliance/rootfs-*, /v1/player/boot-health, operator releases; central/releases.py, contracts/release.py boot-ticket types (KEEP MAX_ROOTFS_BYTES), registry ticket branch, _report_boot_health, appliance/updates.py; reduce bootstrap.py to mount_root + helpers netboot_init needs | yes | open |
| p4-retire **s6** (tables) | DROP migration for appliance_* tables | migration | open (LAST, irreversible; owner-authorized) |

**Boot-chain decisions (2026-09-12, owner-delegated technicals; owner approved "wire boot chain + retire"):**
- **Kernel = Raspberry Pi `linux-image-rpi-2712` + `raspi-firmware`** (Pi 5 = BCM2712; ships Pi 5 DTBs + SPI-EEPROM bootloader firmware), NOT Debian generic `linux-image-arm64`. rpi-image-gen squashfs has NO kernel/`/lib/modules` (metadata-only device layer) — kernel is built in a SEPARATE scratch root, never baked into the RAM-root squashfs.
- **Initrd = initramfs-tools + `mkinitramfs`** in that scratch root (supplies `/scripts/functions` `configure_networking` + `mountroot` + `$rootmnt` that netboot_init.py:87 hard-depends on), NOT a hand-rolled cpio.
- **Transport split (decided):** kernel + initrd over TFTP; large squashfs over HTTP. `base_url`/`base_sha256` injected by operator boot server via cmdline.txt, never baked.
- **Init closure (empirically verified, 7 files):** netboot_init + appliance/{__init__,bootstrap,provision} + contracts/{__init__,release,equipment}. mDNS/zeroconf/ifaddr/updates.py/GTK deliberately absent (lazy imports never reached on this path).
- **Open (settle in s2b, not blocking s2a):** exact Pi 5 TFTP firmware filename set — confirm vs RPi network-boot doc (0009 Sources). Content-verified only until s3 (QEMU) + owner Pi.

## Residuals (follow-up, not blocking)
- CI Immich-fixture is flaky (`docker_command_failed` starting the container) — recurs on ~1/3 of
  e2e runs, clears on re-run. Worth a retry/hardening pass on `scripts/immich_fixture.py`.
- **Single source of truth for bootstrap's contracts/appliance module set** — currently duplicated across 4 sites (wheelhouse `ROOTS`, dist-packages copy, initramfs hook, `verify_initramfs`). Collapsing this kills the failure class that caused three CI cycles here.
- Health-JSON `persistence` (`player/service.py` `_write_health`) is hardcoded `"volatile"`
  even for a D0/persistent player — telemetry-only inaccuracy; docs describe actual behavior.

## Errata
Append-only spec contradictions found during implementation live in `.claude/errata.md`.
