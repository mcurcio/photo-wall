# Signed userspace release and A/B contract

Implemented and tested release/slot contract, 2026-09-05; physical boot and power-loss qualification remain pending. This implements the fixed boot-ABI scope in [decision 0005](decisions/0005-native-platform-and-registration-fallback.md). The common bootstrap, updater and build tooling share the stdlib-only `contracts.release.Release`; never duplicate manifest parsing in shell.

Canonical manifest bytes are sorted, compact UTF-8 JSON with exactly one trailing newline and exactly six fields: `schema=1`, clean 40-character Git `revision`, SHA-256 `boot_abi`, `configuration_sha256`, `rootfs_sha256`, and integer `rootfs_size` from one byte through 1 GiB. No arbitrary artifact path or URL is accepted. The only public rootfs artifact filename is `rootfs-<sha256>.squashfs`. A release ID is SHA-256 of canonical manifest bytes. Detached signatures are 64 raw Ed25519 bytes over those exact bytes. The deployment's public verification key is common public configuration; private signing keys and image binaries remain outside Git.

The caller verifies the signature before trusting parsed fields, using the pinned OpenSSL Ed25519 verifier in both userspace and initramfs. Both reject a different boot ABI or public configuration hash before download/selection. Rootfs byte length and SHA-256 are verified before RAM mounting on every boot. Build records additionally retain the upstream input signature/checksum, package and Python inventories, file hashes, tool versions and public configuration bytes. The signed rootfs digest covers its embedded inventories. Boot ABI identifies the exact fixed kernel, modules and firmware/DTB set; a userspace update cannot replace these components.

The updater operates only within an existing Photo Wall-owned state directory under an exclusive lock. It stores two complete release slots plus at most one bounded incoming candidate. It never formats storage or evicts Player cache pins. Check free space for the full incoming rootfs and a 64 MiB safety margin before transfer; an insufficient-space result preserves both complete slots. Reject symlinks, nonregular files, oversized manifests/signatures/images and partial/hash-mismatched/signature-mismatched downloads. Private atomic writes and directory fsync protect metadata. Interrupted incoming files can be cleaned only under the updater's owned paths.

State distinguishes the last validated active slot from a pending trial slot. Staging a verified candidate fills the inactive slot and writes a pending record atomically. Before attempting the candidate, the bootstrap durably consumes its single trial attempt. A reboot before explicit health acceptance selects the previous active slot. Candidate verification/startup failure selects the previous validated slot. Mark-good changes the active slot only for the release selected during this boot, after state persistence, native initialization and central authority reconciliation remain healthy for the configured trial interval. A stale health report cannot validate another release or another boot. No old execution authorization is restored by rollback; Player startup always obtains a new enrollment epoch.

The same verified rootfs bytes are copied into RAM and mounted read-only with a bounded writable tmpfs overlay; updates do not mutate the running filesystem. Persistent storage holds generated identity/cache/update artifacts only. A device without usable owned state may boot the signed common network release and register its volatile storage fault, but cannot perform durable A/B updates or playback. The bootstrap and original network release remain available independently of per-device state.

Tests must exercise corrupt signature/hash, different ABI/config, truncated download, disk pressure, interrupted state writes, failed trial/reboot, stale health acceptance and identity/cache preservation. A generic VM boot of the same rootfs is userspace/bootstrap evidence only; exact Pi PXE/HDMI qualification still requires the physical bench.

## SlotStore API and durable layout

`appliance.updates.SlotStore(state_root, public_key, boot_abi,
configuration_sha256)` requires the existing regular `.photo-wall-state-v1`
marker with exact `photo-wall-state-v1\n` contents. PWSTATE root must belong to
uid 0 and have mode 0755 or0711; its marker is root-owned and read-only. The
`updates/` directory is root-owned mode 0700, allowing the separate uid 10001
Player to traverse to its own private subtree without accessing update state.
The store owns only a private
`updates/` subtree. Public `verify_release` is the common stdlib/OpenSSL verifier
for bootstrap and userspace: bounded raw signature/manifest inputs, public PEM
key, signature verification before canonical decoding and ABI/config checks.

The store serializes each operation with a nonblocking exclusive lock held over
verification, staging or boot selection; concurrent attempts fail closed. A
staged rootfs is streamed to a single incoming candidate, checking length before
every write and hash before publication. After fsync it replaces only the
inactive slot, and atomic metadata references the exact release ID. A crash
between slot publication and metadata leaves no new execution authority.

`select_boot(boot_id)` consumes a trial durably before returning
`BootSelection(release, path, slot, trial, boot_id)`. Repeated selection in the same
boot is idempotent. `reject_boot(release_id, boot_id)` rejects only the current
selection; bootstrap can then select the verified active slot within that same
boot after a candidate copy/mount failure. Rejecting the active slot suppresses it
for that boot and yields the common network fallback. New boots reverify active
bytes. With no active release, an unsuccessful first trial returns no selection;
it never promotes itself merely because no previous release exists.

`mark_good` requires the current trial's exact release and boot identity. The CLI
additionally observes `/run/photo-wall/service-health.json` continuously for 30
seconds, using the actual Linux boot ID and monotonic time. It requires fresh,
increasing samples with durable persistence, healthy state, a stable Player ID
and current positive authority epoch. Invalid/missing/stale/unhealthy samples or
identity changes reset that interval. The local API stays available to the
bootstrap/test harness; only the CLI performs service health acceptance.


The updater's fixed internal paths are `updates/{A,B}/manifest.json`,
`manifest.sig` and `rootfs.squashfs`, plus a private JSON state file and lock.
Internal slot names never come from a manifest. `incoming/` contains at most one
rootfs and the two bounded metadata files. A verified staged candidate replaces
only the inactive slot before metadata publishes its exact release ID. Staging
refuses to replace a selected, unaccepted trial, so bootstrap's returned path
remains protected until acceptance or explicit rejection. Unknown files,
symlinks, nonregular files and hardlinked files fail closed; cleanup never ranges
outside those owned names. Updates do not modify `player/` identity or cache. Staging the exact already-active
release is idempotent after re-verifying its cached bytes; it does not create
ambiguous trials with the same release ID in both slots. That no-op preserves any
different pending release; it does not act as cancellation.

OpenSSL receives only bounded private temporary files and fixed arguments. The
common public key must be an Ed25519 SubjectPublicKeyInfo PEM; a different key
algorithm is rejected. Verification has a 10-second subprocess bound. The
production executable is `/usr/bin/openssl`; no command, path, URL or executable
from a signed manifest is evaluated. The verifier authenticates bytes before
calling the shared canonical parser.

The CLI is `python3 -m appliance.updates`, with required `--state-root`,
`--public-key`, `--boot-abi` and `--configuration-sha256` arguments. `stage` takes
local `--manifest`, `--signature` and `--rootfs` files. `select` emits a JSON
selection using the actual Linux boot ID; `reject --release-id` releases only the
matching selected boot. `mark-good --release-id` requires the bootstrap's
root-owned mode 0600 `/run/photo-wall/boot.json` to identify that same successful,
durable trial with no boot fault, before observing service health. Its poll
interval is 250 ms, maximum sample age/gap is2 seconds, acceptance interval is30 seconds
and total waiting bound is180 seconds. A common fallback, previous boot report,
volatile registration or stale healthy file cannot accept the candidate.

## Executed evidence

On 2026-09-05, `.venv/bin/python -m pytest tests/test_updates.py tests/test_release.py -q`
passed **62 tests** (41 updater and 21 shared release checks); scoped Ruff and
relative-document-link checks passed. The tests exercise real Ed25519 signatures,
canonical parsing order, ABI/config mismatch, selected-trial protection, same-boot
rejection and next-boot fallback, no-active first trial, truncated/corrupt/oversized
streams, disk pressure, concurrent attempts, interrupted state publication,
symlink rejection, identity/cache preservation, boot-report identity and sampled
health success/failure. Tests on macOS explicitly substitute the synthetic
fixture owner for production uid 0 and use the installed OpenSSL 3.6.0 because
macOS's `/usr/bin/openssl` is LibreSSL; production defaults remain unchanged.

A separate isolated Ubuntu arm64 container smoke exercised the current stdlib
updater with actual uid 0-owned state and `/usr/bin/openssl` **OpenSSL 3.0.13**. It
generated temporary Ed25519 keys, signed and staged a base/candidate, consumed a
trial, accepted the base, rejected the next trial and selected the validated base
within the same boot. The container had no network or host mounts. This verifies
the production verifier path and Linux filesystem/locking calls, using synthetic
payload bytes; it is not a SquashFS mount, real image boot, forced power cut or
physical update/rollback result. Those remain the [platform build and boot
acceptance gates](module-appliance-platform.md).
