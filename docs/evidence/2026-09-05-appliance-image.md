# Appliance image build evidence — 2026-09-05

Status: final signed image and PXE bundle built, authenticated and reopened successfully. Generic VM boot, fresh PXE and physical Pi 5 qualification remain pending. The image and bundle are outside Git at `/Volumes/Dock/PhotoWallArtifacts/2026-09-05-01a0731f/final-073f57d`.

## Exact inputs and identities

The build used clean committed source revision `073f57d7547507de452a2fd273088d7542a6d070` with source epoch `1788652551`. The verified Canonical Ubuntu 24.04.4 Raspberry Pi arm64 input was SHA-256 `790652faeb4f61ce7bb12f5cb61734595c61d3cd882915b8b5f9918106c80d37`, size `1257196128` bytes. The package additions were resolved from snapshot `20260905T000000Z`.

The externally retained Player-only package evidence is `/Volumes/Dock/PhotoWallArtifacts/2026-09-05-01a0731f/player-073f57d`. Its inventory SHA-256 is `0c88d78d6f4bffe5002080c4b01b8aea4f54979154736eecb491019195aede02`; its source archive SHA-256 is `19412f21482ee027b9e21e7e918210c203e14b3031a0445f9e7e49a72735a765`; its locked requirements SHA-256 is `8fab491c6e46ae1b55e2382549839ab4a334b18800ca2230ff3479082cd20ba7`; and its 16-wheel bundle includes the Player wheel SHA-256 `b94189d44abb4987254ea31fa60a3910adfcc23e54bedb11e13935eb335b9ba5`. The recorded target is CPython 3.12.3 on Linux AArch64 with glibc 2.39. The Player inventory revision matches the appliance revision.

The retained package evidence recorded 628 baseline packages and 912 packages after the runtime closure, with 397 downloaded Debian packages. The evidence file identities are:

- `base-packages.tsv`: SHA-256 `24552902c4410ce5f256106d22935504efaa4ec2b79897d1ea663595ed4c7b5c`.
- `packages.tsv`: SHA-256 `778fef6e79932eccf914675dbad62a21fe6d8324cd3175544c31da2a29e24981`.
- `deb-hashes.json`: SHA-256 `1d8422ccd5230e29a607bee253a00abeab681d682711a0cdf44573902e39a99c`.
- `python-packages.json`: SHA-256 `0dd5bfb8b984861e362b5fd789f2d473bddae0ed64be119a02ae04f43d296a6a`.
- `firmware-profile.json`: SHA-256 `e4be5931c3e3b1499fc8350abce6bff355219fac46fd515a600bef1439294a00`.

The public configuration digest is `bafd0eb024cd5c0e309c6ed38b6a429fd8e7bbae1ced4ebe1e783136f119fcff`. The fixed boot ABI is `08c70572e50f81b77b9103888c3d8275f3357a6a153bceb34f5c599d3481c2fd`.

## Build and finalization

The unsigned preparation ran in the task-owned Linux container from the immutable exported source, with the source guard active:

```text
cd /work/release-source-073f57d
PYTHONDONTWRITEBYTECODE=1 python3.12 -m appliance.build prepare \
  /work/base-root /work/release-source-073f57d /work/player-073f57d \
  /work/public /work/package-evidence /work/unsigned-073f57d
```

It produced the canonical release manifest and rootfs:

```text
release.json SHA-256: f8322e1329a1dc8e11f66f3c31bb4fa074f9d1638c4dd82b9e8b925bfa78bb60
release ID:           f8322e1329a1dc8e11f66f3c31bb4fa074f9d1638c4dd82b9e8b925bfa78bb60
rootfs:               rootfs-39dfacd85871c51d81934c45b785f8a3e1cbf8d4d36a1601042e0719a2dcc413.squashfs
rootfs size:          672448512 bytes
rootfs SHA-256:       39dfacd85871c51d81934c45b785f8a3e1cbf8d4d36a1601042e0719a2dcc413
```

The release ID above is the SHA-256 of the canonical six-field manifest. The generated rootfs was reopened with `unsquashfs`; initramfs contents were checked for the required bootstrap, updater, release modules and boundary exclusions; the Player virtual environment passed the native GTK/GStreamer import and decoder-factory smoke; and the Pi 5 firmware profile retained its declared link closure. The unsigned build report SHA-256 is `ece7dbef62ae537e02287205abd2f2bee7da4cfb04c7ac065dc493b3068c2096`.

The detached raw Ed25519 signature was copied from the externally staged public signature at `/Volumes/Dock/PhotoWallArtifacts/2026-09-05-01a0731f/unsigned-073f57d/release.sig` to the container as `/work/release-073f57d.sig`. It was 64 bytes with SHA-256 `8499e902e4837eb7c0987b5d43fa18713605695379d66b51243becb6d675afbb`. Finalization used the original public deployment inputs as the independent trust anchor; it did not use or copy private keys:

```text
cd /work/release-source-073f57d
PYTHONDONTWRITEBYTECODE=1 python3.12 -m appliance.build finalize \
  /work/unsigned-073f57d /work/release-073f57d.sig /work/final-073f57d \
  --trusted-public /work/public
```

Finalization authenticated the detached signature, checked the release/rootfs bytes and public configuration, reopened the signed rootfs to compare its staged boot tree, created the FAT/ext4 disk, and reopened the completed disk through read-only libguestfs verification. The generated artifact is:

```text
image:                photo-wall-pi5-073f57d7547507de452a2fd273088d7542a6d070-bafd0eb024cd5c0e.img
image size:           5906628608 bytes
image SHA-256:        d822bc819606c63a9fdcf40b03d6a65933d7881326757de0b8b2a34ae94b3448
artifact.json SHA-256: 6b7afe180e14d9c91959e4536d6c8c48968767a8db3a456e091ffffa2e6f90a5
SHA256SUMS SHA-256:   064edb5c7a89ce6d58a653388a8b26a916d56bb02a35e1b594e94ff409ae1139
```

The PXE tree contains 370 hashed files totaling 776,063,135 bytes. The SHA-256 of its canonical sorted inventory is `b38fcc4760ccb0b57fcbe5f18ff696a24fbad1f7bac52212bf791a0ceed7d723`. The final folder was copied to the external handoff path above, and an independent host read produced the same image size and SHA-256. The retained Linux test log is `linux-tool-tests.log`, SHA-256 `f6742aea30edeb0d6d061c9cf3b11a21d7bc56881440d3fe1eba2465f04f57fb`.

The latest copied source test harness ran the requested real Linux tooling suite with `PHOTO_WALL_IMAGE_TOOL_TESTS=1` and `--noconftest`: **138 passed in 65.36 seconds** across `test_appliance_build.py`, `test_bootstrap.py`, `test_updates.py` and `test_release.py`. The test harness used a separate copy of the current source because the container's older convenience copy was stale; the immutable export used by preparation remained unchanged and passed the builder's executing-source identity check.

## Qualification boundary

This evidence establishes a checksum-identified signed image, matching PXE tree, package closure, Player-only wheel installation, native import smoke, SquashFS and filesystem-tool verification, signed boot-tree binding and final copied-image integrity. It does not claim a generic VM boot, DHCP/TFTP reachability, Pi 5 EEPROM/PXE boot, dual HDMI scanout, seat permissions, thermal performance, visible timing or physical rollback. Those require the separate Linux VM and physical bench gates. The image, PXE bundle, signature and package inputs remain outside Git; the original public anchor at `deployment/public` was byte-identical before and after finalization, and `deployment/private` was not used or modified.
