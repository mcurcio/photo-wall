# CI appliance image and VM gate

The ARM64 workflow builds the Raspberry Pi appliance from the exact checked-out
commit on `ubuntu-24.04-arm`. An always-running routing job selects the retained
builder and installed OS base by definition. Application-only changes reuse
those inputs. A changed definition or explicit preparation request can build a
missing candidate. Registry failures and incompatible inputs stop the job.

The [reusable OS base decision](decisions/0007-reusable-os-base.md) owns this
separation. APT runs during builder/base preparation. Final assembly restores
verified inputs and runs in a container with `--network none`. PXE and the RAM
bootstrap still deliver signed releases; Players never update through APT.

The prepared-input assembly command, run inside the selected builder, is:

```sh
python3 -m scripts.build_ci_image \
  --repository /workspace/photo-wall \
  --revision <40-character-commit> \
  --output-dir /work/photo-wall-artifact \
  --deployment-dir /work/photo-wall-deployment \
  --os-base /work/photo-wall-os-base \
  --os-base-builder-image ghcr.io/mcurcio/photo-wall-builder@sha256:<digest> \
  --os-base-image ghcr.io/mcurcio/photo-wall-os-base@sha256:<digest> \
  --player-package /work/photo-wall-player \
  --central-image sha256:<central-image-id> \
  --builder-image sha256:<builder-image-id>
```

The OS base and Player package must be prepared before entering the offline
container. The builder's registry digest identifies its published content;
`--builder-image` retains the local Docker image ID used by the existing VM
harness. A local candidate can omit `--os-base-image` until publication.

On the disposable Linux runner, the workflow runs the Player unit preflight
immediately after checkout, before container builds and image assembly:

```sh
sudo python3 scripts/check_player_unit.py
```

This command requires root and PID 1 systemd 255. It refuses to mutate a host
where the `wall` account/group or UID 10001, `/run/photo-wall`,
`/run/user/10001`, or `/var/lib/photo-wall` already exists. It creates a
uniquely named temporary unit from the checked-in
`appliance/systemd/player.service`, retaining its sandbox and production
`ExecStartPre` while replacing only dependencies, type, restart policy,
timeout, and the test `ExecStart`. The probe runs as UID 10001 and checks the
RuntimeDirectory-created writable leaf, the protected boot record, the
Wayland socket, read-only runtime state, and hidden `/home` and private `/tmp`
canaries. It emits only a fixed JSON status and removes only resources created
by that run. This is a fast systemd contract check; it does not build an image,
boot a VM, or qualify Raspberry Pi hardware. A live runner result must be
recorded separately from the portable unit-rendering tests.

The output directory contains the finalized signed disk during the e2e step,
then its compressed `.img.xz` upload form, PXE bundle, `artifact.json`,
`ci-image.json`, and `generic-boot/`. `ci-image.json` records
the source commit, central, worker and builder image identities, disk size/hash,
release identities, and the generic kernel/initramfs paths. Its three runtime
paths (`bundle`, `generic_boot`, and `deployment`) are absolute because the
e2e harness consumes the manifest on the same runner before artifact upload.
The deployment directory is separate from the upload tree and contains
disposable CI TLS/signing material plus the signed rollback candidate bundle.
Its path and public accepted/candidate release metadata are recorded under `rollback_candidate`.
The workflow uploads the output tree and a
sanitized e2e report; it never uploads that private directory.

These are qualification images configured for `https://photo-wall.test` with
new disposable trust keys and a one-day TLS certificate. They are not configured
for an operator's deployment. Production images require that deployment's
public origin, CA and release trust anchor; no production signing secret is
made available to pull-request code. The [boot gate](module-appliance-e2e.md)
defines the scenarios and their limits.

The build phases remove the compressed Ubuntu input after decompression unless
`--base-cache` is supplied, then remove the raw base image, extracted root,
source export, player wheelhouse, and package staging after each consuming
phase. Sparse decompression preserves the raw image bytes without allocating
whole zero-filled blocks on Linux. These measures reduce peak workspace use;
the [first hosted build](evidence/2026-09-05-github-image.md) recorded about 106 GiB
initial free space and completed assembly; its peak usage was not measured. Every generated output
is a new regular-file-backed path outside Git. A failed fixture setup or build removes its newly created private deployment
and temporary workspace; preexisting paths are refused and preserved. Successful
builds remove the signing key and retain the disposable TLS fixture and signed candidate until
the job finishes. No private deployment file enters the artifact directory.

Before fetching inputs, the orchestrator records the runner's measured free
space and requires at least 9 GiB, including room for the private rollback
candidate's bounded 1 GiB compressed rootfs. The workflow does not delete unrelated SDK
or tool caches to manufacture capacity; it relies on the explicit phase
cleanup and the runner's available workspace.

## Reusable build inputs

`scripts/os_base.py` owns the installed baseline format: `manifest.json`, a
metadata-preserving `root.tar`, and `evidence.tar`. It validates the definition,
archive hashes, bounded archive structure, native package inventory, and boot
files before admitting a root. Restoration preserves ownership, permissions,
hardlinks, symlinks, xattrs, ACLs and device nodes. Configured Photo Wall roots
and private material are rejected. A failure never triggers a cold fallback.

`appliance/os_definition.json` is authoritative for Ubuntu image pins,
architecture, snapshot and requested native packages. OS preparation code and
builder recipe/tool inputs participate in its fingerprint. Application source,
Python runtime locks, deployment configuration and final assembly changes do
not. Boot scripts and release contracts are applied downstream and regenerate
boot compatibility evidence independently of the native dependency identity.

`scripts/ci_images.py` owns registry selection. Definition tags locate an
artifact; the job resolves the tag once and uses its immutable registry digest.
Candidates are prepared only for changed definitions or an explicit preparation
request and are published after the appliance gate passes. Fork candidates
remain local to the job. GitHub Container Registry is the durable source;
BuildKit/GHA caches remain optional accelerators. Registry authorization or
transport errors are not interpreted as artifact absence.

The application wheelhouse is built from the current Git commit before final
assembly. Its [offline admission](module-player-package.md#prepared-input-for-offline-image-assembly)
reconstructs the expected package using only supplied dependency bytes and
compares the complete result to the committed source and lock. The final
container has no network, so unexpected dependency acquisition cannot succeed.
The final `ci-image.json` retains the OS baseline manifest and optional registry
reference alongside the application revision and release identities.

Central and worker builds retain their existing independent BuildKit scopes.
The builder instead uses its own definition and `appliance/build-tools.txt`,
which avoids invalidating the image when the application `uv.lock` changes.
The OS carrier image contains the prepared archives under `/os-base`; Docker
transports the files, while the canonical archive restore preserves the Pi
filesystem's metadata.

Legacy local cold-build options (`--base-cache`, `--extracted-base-cache`,
`--apt-archive-cache`) remain available for diagnostics and explicit preparation.
They cannot be combined with prepared-input mode and are not used by ordinary
GHA final assembly. Earlier [cache measurements](evidence/2026-09-05-ci-cache.md)
and [APT failures](evidence/2026-09-08-ci-package-acquisition.md) describe the
preceding implementation, not measured performance of this replacement.

Every final assembly still binds current source/configuration, generates and
signs boot artifacts, reopens the disk, and runs the exact-artifact boot gate.
Build and boot share one runner so private disposable fixtures need no transfer.

The workflow uses scoped package publication permissions, pinned action commit SHAs, push-to-main,
pull-request, and manual triggers. Push and pull-request runs always select the
`smoke` scope. A manual dispatch presents an explicit `scope` choice, defaulting
to `smoke`; selecting `full` conditionally builds and loads the production ARM64
media-worker image. Its immutable Docker image ID is recorded in `ci-image.json`
and passed back to the VM harness. The full harness rejects a missing or changed
worker identity before creating fixture state, so a requested native-media run
cannot degrade into a non-media image test. Dispatch it for a specific revision with
`gh workflow run appliance.yml --ref <revision> -f scope=full`; the resulting run
must pass before its report is cited as full appliance evidence. Each PR keeps one image run active and the
latest revision pending, so benchmark pushes do not cancel a costly build
already underway; superseded pending revisions are replaced according to
[GitHub concurrency semantics](https://docs.github.com/en/actions/concepts/workflows-and-actions/concurrency). It runs the root-owned
`scripts/test_appliance_e2e.py` against the exact disk and generic boot
directory recorded in `ci-image.json`, using the selected scope.
That test requires signed accepted-release identity, a successful RAM-root boot,
fresh-session production Player enrollment, and unchanged boot-disk/protected boot evidence.
Controller, worker, and Player behavior runs in the separate full software E2E workflow;
the manual full appliance scope adds exact-image native media, machine restart, and
central rollback qualification in the same job while its raw disk and private
fixture artifacts remain available.
The smoke report records generic VM and
physical Pi/HDMI/PXE qualification separately; a passing workflow does not
claim those hardware results. After e2e, the workflow compresses the raw disk
in place and writes `UPLOAD-SHA256SUMS`; the raw disk is not uploaded, avoiding
a second multi-GB copy in the runner workspace and artifact service.

The builder image installs `git`, `gnupg`, `initramfs-tools-core`, and
`python3-packaging` alongside the existing libguestfs, ARM QEMU, filesystem,
and initramfs tooling. It then installs the exact locked Packaging 26.3 wheel
with its SHA-256 for the package builder only; this tool environment is never
copied into the appliance runtime. Ubuntu package resolution remains pinned to the dated snapshot in
`appliance/os_definition.json` and the builder Dockerfile. No repository private key,
operator credential, or upstream connection secret enters the build context.

The derived upstream fixture explicitly uses the daemon's default builder for
its already-loaded central parent. A selected container-based Buildx builder
cannot see that local parent tag. The isolated reproduction and cache-gate
correction are recorded in [media evidence](evidence/2026-09-06-vm-media.md).
