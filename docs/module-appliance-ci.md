# CI appliance image and VM gate

The ARM64 workflow builds the Raspberry Pi appliance from the exact checked out
commit on `ubuntu-24.04-arm`. It first builds the central image and the pinned
Linux appliance builder, then runs `scripts/build_ci_image.py` inside that
builder. The orchestration calls the existing package, Ubuntu input,
appliance, signing, finalization, and generic-initramfs builders; it does not
reimplement any image format or boot logic.

The command is:

```sh
python3 scripts/build_ci_image.py \
  --repository /workspace/photo-wall \
  --revision <40-character-commit> \
  --output-dir /work/photo-wall-artifact \
  --deployment-dir /work/photo-wall-deployment \
  --base-cache /work/photo-wall-base-cache \
  --central-image sha256:<central-image-id> \
  --builder-image sha256:<builder-image-id>
```

The output directory contains the finalized signed disk during the e2e step,
then its compressed `.img.xz` upload form, PXE bundle, `artifact.json`,
`ci-image.json`, and `generic-boot/`. `ci-image.json` records
the source commit, central and builder image identities, disk size/hash,
release identities, and the generic kernel/initramfs paths. Its three runtime
paths (`bundle`, `generic_boot`, and `deployment`) are absolute because the
e2e harness consumes the manifest on the same runner before artifact upload.
The deployment directory is separate from the upload tree and contains only
disposable CI TLS/signing material. The workflow uploads the output tree and a
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
builds remove the signing key and retain only the disposable TLS fixture until
the job finishes. No private deployment file enters the artifact directory.

Before fetching inputs, the orchestrator records the runner's measured free
space and requires at least 8 GiB. The workflow does not delete unrelated SDK
or tool caches to manufacture capacity; it relies on the explicit phase
cleanup and the runner's available workspace.

The workflow has `contents: read`, pinned action commit SHAs, push-to-main,
pull-request, and manual triggers. Each PR keeps one image run active and the
latest revision pending, so benchmark pushes do not cancel a costly build
already underway; superseded pending revisions are replaced according to
[GitHub concurrency semantics](https://docs.github.com/en/actions/concepts/workflows-and-actions/concurrency). It runs the root-owned
`scripts/test_appliance_e2e.py` against the exact disk and generic boot
directory recorded in `ci-image.json`. That test requires signed release
identity, headless durable enrollment, restart identity continuity, central
rejoin, and unchanged disk/protected boot evidence. It reports generic VM and
physical Pi/HDMI/PXE qualification separately; a passing workflow does not
claim those hardware results. After e2e, the workflow compresses the raw disk
in place and writes `UPLOAD-SHA256SUMS`; the raw disk is not uploaded, avoiding
a second multi-GB copy in the runner workspace and artifact service.

The builder image installs `git`, `gnupg`, `initramfs-tools-core`, and
`python3-packaging` alongside the existing libguestfs, ARM QEMU, filesystem,
and initramfs tooling. It then installs the exact locked Packaging 26.3 wheel
with its SHA-256 for the package builder only; this tool environment is never
copied into the appliance runtime. Ubuntu package resolution remains pinned to the dated snapshot in
`appliance/build.py` and the builder Dockerfile. No repository private key,
operator credential, or upstream connection secret enters the build context.
