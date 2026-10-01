# Player node full base and PXE build — 2026-09-30

**Verified local dirty-source artifacts, not release certification or physical boot.**
This closes the missing full production squashfs/kernel/initrd/PXE artifact build
for the exact `components-stopfix` snapshot below. It also records a successful
real stage-1 mount probe using the shipped initrd. It does not replace the
[resumption lifecycle evidence](2026-09-30-player-node-resumption.md), qualify
physical Pi/display behavior, or approve any boundary still pending in the
[handoff](2026-09-30-player-node-handoff.md).

## Source and artifact identity

Authoritative checkout: `/Users/matt/.codex/worktrees/readiness-design/photo-wall`.
The build consumed its frozen dirty snapshot at
`/Volumes/Dock/Temp/photo-wall-node-resume-20260930/components-stopfix/source`,
with the matching immutable component packages mounted read-only. No commit,
deployment, production command, physical boot or privileged shared-CI run occurred.
Git HEAD `0a9b530caae00757ebae66b74b47adb38f1a5fb0` identifies only the parent
revision. The source inventory, not HEAD, identifies the packaged working tree.

| Input | Exact identity |
|---|---|
| Source inventory SHA256 | `793834f315daafaee704a5e59fc2a29748417615a02a500cd1bf5182531b9801` |
| `components.json` SHA256 | `3f6d1997dc1e88a5da3e8b0692967fd0437cab970d89327ea37e15ee481852de` |
| Node base ABI | `node-v2-90790aa555a9879a348efab6098d6c65dd31b1b3b833a561bed96f242e51e938` |
| Graphics ABI | `weston14-3734f476404779775c755b829a6583bcfeb9142a52f3c30a6b15b97709e3f31f` |
| Plugin ABI | `frame-v3` |
| Player environment SHA256 | `34eeb17b00ba7b2d330beafcecfd2868c6d94a9b45be54fbd130aa64e974d1d7` |
| Manager environment SHA256 | `4206a47cb9d8fd659cc7f409fd2b44b97e4ee3f2cb4ab5550cc9ab3581ec3e48` |
| Debian snapshot | `20260904T000000Z`, epoch `1788480000` |
| rpi-image-gen revision | `262d4df5a9f9d4133370465399a7958a7c22cdc7` |
| Parent HEAD clock floor | `1790788076` |

The bootstrapper was staged through the existing closure, content-derived
version, `stage_tree`, `assert_minimal`, and package helpers in
[build_bootstrapper_deb.py](../../scripts/build_bootstrapper_deb.py).
Its normal revision build entry point and the node-components revision entry
point were deliberately not used: both archive committed source and would omit
these working-tree changes. The wrapper verified every recorded source hash,
mode and symlink, component inventory identity, and coherent component ABI.
Original Git metadata was mounted read-only for the unchanged bundle assembler's
HEAD clock-floor computation. It was not used as the content identity.

Artifacts and full inventories are retained under
`/Volumes/Dock/Temp/photo-wall-node-resume-20260930/full-base/artifacts-stopfix`.
Both `photo-wall-base-bundle` and the explicit V2 `photo-wall-node-bundle` exist.
The latter carries the production `photowall.node=v2` cohort selection. Their
squashfs, kernel and initrd bytes are identical; their command-line/checksum
manifests differ as intended.

| Artifact | Bytes | SHA256 |
|---|---:|---|
| `photo-wall-base.squashfs` | 325976064 | `a3952fe8c2cb0be37d3617dc0e0a16ef06f5c74490b2fe9945955c70f8c13bca` |
| `boot/kernel_2712.img` | 10197590 | `154f0ea1e14bb1075077dfd1bdc63c1502bc0b17b0354628d515b4fbd3a4198d` |
| `boot/initrd.img` | 26606583 | `dc062e477f4346bf9fb61edeed6f3c5180dc842244fb2b56165e080071da6711` |
| `boot/pieeprom.upd` | 2097152 | `221db65c1ac708c7121321505c19bedcad167b4d2461dce5fe08fea91906d568` |
| `boot/pieeprom.sig` | 80 | `fd8dc1057af8361a4bf1c59874883989e6f9f5bdafc83445422ab9ec3d5b80ea` |
| Base bundle `SHA256SUMS` | 38968 | `6e8fc7ccb71190eadc5d33cccb1218e1f9f1928d2a2f1b0a3a9aa20fd6fbc1d6` |
| V2 bundle `SHA256SUMS` | 38968 | `fc320cd8b51b75c1b7f2ba7d9028fef67764c14202b87e41a8fcf69daf5f7a47` |
| `kernel-packages.tsv` | 4757 | `99b9d7e72a648d8c6a2dcd8c6964354b69d98f9d77874876721db740b87c4d84` |
| `kernel-files.sha256` | 45907 | `15bed9f18333f7e459caa07a3041c3e5716694986519f00dd4b06d8b73137a3b` |
| `artifact-inventory.json` | 212082 | `2bc742143e285e3bbc7af89b9cce124d95a197428f93aada2149a42db41b8708` |

The inventory covers all exported overlays, manifests, ABI sidecars, diagnostics,
workflow bodies and component/source records. The kernel was freshly installed
from the existing declaration: `6.18.50+rpt-rpi-2712`; packaged EEPROM input was
`pieeprom-2026-09-25.bin`. Historical kernel images were not reused. The Raspberry
Pi archive remains the declaration's current archive, not an immutable snapshot;
actual package versions and artifact hashes are recorded without claiming more
reproducibility than these inputs establish.

## Tool environment and recovered attempts

The native arm64 Docker VM provided 8 CPUs and about 7.65 GiB RAM. A new exclusive
volume `pw-node-base-resume-20260930` held the Linux chroots and build files; Dock
scratch held recipes, logs and exported artifacts. Existing volumes/caches were
not replaced or deleted. Source and components were always read-only.

- Tool parent image: `sha256:03031bc30a32048aa4ae3022bf3d8bd8636ac5cf8639695022761f6aeb23f001`.
- Final tool image: `sha256:2e6e0f1ecf7946bd10877069898fa9958881e4fd665702f0a8ae66aa89208a9e`.
- The production builder digest, exact Debian snapshot and pinned rpi-image-gen
  were used. Frozen, hashed non-development `uv.lock` dependencies were installed
  separately under `/opt/photo-wall-build-python`. Debian Python and its tool
  packages were retained. The wrapper refuses changed `uv.lock`/`pyproject.toml`.
- Dockerfile `FROM` required a local tag for the locally built parent. Its exact
  image ID was checked before build and recorded; the mutable tag alone is not
  a reproducibility claim. A raw local `FROM sha256:...` attempt failed resolution.
- Upstream describes container builds as outside its formally supported native
  host configurations. These local successful probes establish this specific
  environment, not general upstream container support. See the
  [pinned upstream README](https://github.com/raspberrypi/rpi-image-gen/blob/262d4df5a9f9d4133370465399a7958a7c22cdc7/README.adoc).

All logs below live in the `full-base` scratch directory. Failures were retained.

| Attempt | Outcome and correction |
|---|---|
| `tool-build.log` | Initial dependency installation hit newer base-image Perl versus pinned snapshot skew. Existing production snapshot-priority/downgrade normalization resolved it; no pin change. |
| `tool-build-normalized.log`, `tool-build-final.log` | Tool image built. Debian interpreter PATH and upstream tool revision/import checks passed. |
| `mount-smoke.log` | Private tmpfs/proc/sysfs/devpts mounts passed with scoped `SYS_ADMIN`; container removed and scratch empty afterward. This alone did not qualify loop mounts. |
| `rootfs-build.log` | Earlier dirfix preflight lacked Pydantic and stopped before staging. Frozen project dependencies were added to the isolated derived image; helper imports passed. |
| `tool-build-project.log`, `tool-build-project-pinned.log` | Raw local-ID Dockerfile resolution failed; inspected exact local-parent tag build succeeded. |
| `rootfs-stopfix-build.log` | Exact bootstrapper/chroot/packages/support tools completed. Genimage's temporary rootfs copy exceeded the container's 1 GiB `/tmp` tmpfs. |
| `rootfs-stopfix-image.log` | Pinned tool's documented `-i` image-only phase resumed from the preserved exact chroot after input hash checks, using new disk-backed temporary storage. Squashfs/SBOM/deploy manifests succeeded. |
| `rootfs-stopfix-verify.log` | Production rootfs content/device checks passed. |
| `kernel-stopfix-build.log` | Fresh kernel install, config check and mkinitramfs passed; mounts were torn down. Local wrapper omitted CI-only `GITHUB_OUTPUT`, stopping before output staging. |
| `kernel-stopfix-stage.log` | Local metadata file supplied; exact selection/copy tail resumed from the completed scratch. Kernel config check and package/file manifests passed. |
| `bundle-stopfix-build-verify.log` | Bundle assembled, but initrd verifier could not find host `zstd`. No verify skip was used. |
| `bundle-stopfix-verify.log` | Added the exact rpi-image-gen-built zstd 1.5.7 support-tool path. Regenerated the manifest through the production closure helper; verified the same assembled initrd bytes, node ABI and complete bundle successfully. |
| `artifact-export.log` | Artifacts exported; temporary export container removed. |
| `initrd-loop0-probe.log` | Real shipped stage-1 mount/module-handover probe passed with one selected loop minor; cleanup verified below. |

The local renderer extracted six exact shell bodies from the frozen
[production workflow](../../.github/workflows/base-image.yml), recording original
and rendered hashes. The adapters were Debian tool Python replacing checkout
`.venv/bin/python`, omission of host apt installation already captured in the
image inventory, and local workflow environment/output paths. Scratch package
installation and production verification logic were retained. Later recovery
wrappers reused exact workflow segments and existing source helpers, preserving
completed artifacts instead of inventing successful earlier exits.

Durable sanitized recipes, adaptation provenance and selected logs are linked from
the [executed full-base support checkpoint](player-node-handoff-support/full-base/README.md#executed-full-base-recipe-checkpoint).

## Acceptance results

The production rootfs check passed required/forbidden paths, packaged bootstrapper
closure, installed Player dependencies, users, exact pinned apt sources,
watchdog/time/resolver restrictions, base ABI and absence of deployment origins
or app trust material. Installed node base/display ABI matched `components.json`.

The fresh kernel config check passed all nine required built-in symbols.
[build_netboot_bundle.sh](../../scripts/build_netboot_bundle.sh) staged 383
overlays and 392 checksum entries. The initrd contains the computed 32-module
stage-1 closure (digest
`b8a9efdeb645bc2f3d20f8010eec9a9c8162668930308565bbb47e4e12c386f0`),
150 CA certificates and the recorded parent clock floor.
[verify_netboot_initrd.py](../../scripts/verify_netboot_initrd.py) passed without
skip flags. Bundle checks verified every checksum, squashfs ABI binding, size
ceiling, firmware watchdog settings and static discovery command line.
The actual packaged DTB with its Pi 5 KMS overlay applied reported vc4, hvs,
pixelvalves, HDMI and v3d enabled. V2 bundle creation and installed ABI checks
also passed.

## Real stage-1 mount probe and cleanup

The production [initrd mount probe](../../scripts/initrd_mount_probe.py) ran
against the exported V2 initrd's identical bytes on the Docker VM kernel. A
separate control-only selector obtained free minor 0 without attaching a file.
The probe container received `SYS_ADMIN`, unconfined seccomp, loop-control
character device `10:237`, and only loop block device `7:0`; no all-loop rule,
host `/dev` bind, Docker socket, physical device or blanket privileged mode.
Device nodes were created only in the disposable container's `/dev`.

The actual allocator was unchanged. If minor 0 became occupied between selection
and allocation, access to another returned minor would be denied. The wrapper
required no selected pre-existing mapping, gave the task image a unique basename,
and guarded any fallback detach with selected-device, basename and exact backing
device/inode checks. Existing backing identities were compared before and after;
only hashes would be logged for unrelated paths.

Observed result:

```text
modules=6.18.50+rpt-rpi-2712 files=293 bytes=5701450
resolve vc4 exit=0 files=23 missing=0 last=vc4.ko.xz
resolve v3d exit=0 files=7 missing=0 last=v3d.ko.xz
before_backing_hashes={}
after_backing_hashes={}
violations=[]
SCOPED_REAL_INITRD_MOUNT_PROBE_PASS
```

The unpredictable marker was read back through the real squashfs/tmpfs/overlay
root. Production teardown reported no remaining mounts; AUTOCLEAR removed the
loop mapping. Selector, probe, builder and export containers exited and were
removed. The task volume and exported artifacts remain intentionally retained.
This establishes stage-1 mount behavior and module **resolvability** on the
Docker VM kernel, not loaded Pi display drivers or physical pixels.

## Remaining qualification and approval boundaries

No Pi firmware/PXE boot, EEPROM update, physical watchdog, physical display,
dual-Output timing or hardware continuity result is claimed. The separately
recorded actual PID1 lifecycle uses a qualified fixture environment; this record
does not claim the full squashfs itself has completed every production-unit
startup/reboot/rollback scenario. Keep that qualification distinct from content
and stage-1 mount checks. Rebuild/reverify affected artifacts if later product
changes alter their input closure or ABI; this snapshot is not silently promoted
to a newer source state.

Pending security decisions, planned bound withdrawal/D16 choices, guard/ledger/
authorization/publication/IaC work and full-source validation remain governed by
the [handoff](2026-09-30-player-node-handoff.md) and
[resumption record](2026-09-30-player-node-resumption.md). This local build does
not authorize publishing, deployment, privileged shared CI or any previously
rejected boundary change.
