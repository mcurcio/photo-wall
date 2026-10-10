# Appliance builder and RAM-root bootstrap

Status: historical, except [The minimal base](#the-minimal-base). The signed builder and its bootstrap were retired. This document follows [decision 0006](decisions/0006-central-authority-and-stateless-players.md) and supersedes the former `PWSTATE` and local A/B design.

**This document describes the signed, combined base+app image (0008 D1), which no longer exists.** [Decision 0009](decisions/0009-minimal-base-and-app-package.md) supersedes it with a minimal, unsigned base image plus the Player as a downloadable `.deb`; the base that replaced it is described in [The minimal base](#the-minimal-base) below. Everything above that section is history, not the mechanism that boots a Node today.

## Ownership and build API

The builder customizes the signature-verified Ubuntu 24.04.4 Raspberry Pi arm64 input, installs the separately locked Player-only package, and emits a common boot disk plus PXE tree. The disk contains one FAT32 `PWBOOT` partition. It has no Player state partition, local rootfs slots, device identity, credentials, execution journal, or authoritative cache.

`python -m appliance.build` provides bounded stages for input decompression, extraction, package installation, exact source export, root preparation, signing finalization, disk creation, and read-only verification. The build records the exact source revision, boot ABI, public configuration digest, rootfs digest and size, boot files, native imports, package inputs, and tool identities. The private signing key stays outside build contexts and upload artifacts.

The boot ABI covers the fixed kernel, modules, firmware, DTBs, initramfs bootstrap, and neutral release parser. A release is compatible only when its signed manifest carries that exact ABI and public-configuration digest. The root filesystem remains a bounded immutable SquashFS artifact stored and selected centrally.

Finalization authenticates the signed release against an independently supplied public key, verifies every rootfs and boot-tree byte, then creates the boot-only disk and PXE bundle. Read-only reopening checks the FAT inventory and proves that no local state partition was added. Linux image tooling tests remain separate from portable generated-byte tests.

## Initramfs flow

The `boot=photowall` initramfs establishes wired networking and time, reads the Pi serial or the fixed VM DMI UUID as operational equipment matching information, and generates a fresh boot request ID. Serial, UUID, MAC, and IP observations are not cryptographic Player identity.

Bootstrap calls central for a boot ticket. Central has already consumed any candidate trial transactionally before returning that ticket, and replaying the same boot request returns the same selection. Bootstrap verifies the ticket binding, authenticates the selected release manifest with the deployment Ed25519 public key, checks the fixed ABI and configuration digest, and downloads the exact rootfs over pinned HTTPS with no proxy or redirect. It validates complete length and SHA-256 while copying the bytes into tmpfs.

For a candidate ticket, trusted bootstrap first arms `/dev/watchdog0` or `/dev/watchdog` with a bounded timeout. The boot command line fixes the Pi watchdog in nowayout mode, and the installed systemd manager configuration takes over at 30-second intervals. A candidate cannot download or enter userspace without this reboot backstop. Pre-root failure returns to the initramfs hook's forced reboot path.

The verified SquashFS is mounted read-only and combined with a bounded volatile OverlayFS. The protected `/run/photo-wall/boot.json` contains only the current boot, device observation, opaque ticket, selected release, trial flag, and `persistence: volatile`. The Player creates fresh session credentials, enrolls with that ticket, receives the current central configuration and assignments, and rejects earlier authority epochs.

Cold boot fails closed when provisioning, time, or central release services are unavailable. Once a process is running, ordinary control-service outages may preserve its currently authorized presentation; no playback promise extends across a reboot.

## Candidate watchdog and rollback

`photo-wall-accept-trial.service` observes the current volatile boot only. The Player sends authenticated health for the exact ticket/session to central. Central alone applies the continuous-health window and promotes the candidate. The local watchdog treats a fresh `release_accepted` response as completion.

If the trial does not become accepted within 180 seconds, the trial observer writes a volatile boot-bound reboot marker and fails. The recovery unit verifies that marker and reboots. If userspace or systemd stops servicing the machine watchdog, hardware resets the machine. Nothing is rejected, promoted, or selected locally. On the next PXE boot, central returns the accepted release because the candidate trial was already consumed.

## Acceptance gates

Portable tests cover canonical release parsing, signatures, configuration bounds, safe files, complete-byte verification, ticket retry binding, mount cleanup, boot-report validation, watchdog timeout, and reboot predicates. PostgreSQL tests cover immutable releases, accepted/candidate policy, once-only trial consumption, idempotent duplicate boot requests, stale health, session binding, promotion, and accepted fallback.

Delivery still requires rebuilding the exact artifact and proving: generic ARM boot identity availability; fresh enrollment without a writable Player volume; current-image native rendering; failed-candidate automatic reboot and central fallback; and physical Pi PXE, replacement, dual HDMI, continuity, and visible coordination. A portable or generic-VM result does not qualify Pi firmware or displays.

## The minimal base

The signed, combined base and app image above, `appliance.build` and the 0009 bootstrapper are all gone; the history stays in [decision 0008](decisions/0008-generic-image-and-serial-identity.md) and [decision 0009](decisions/0009-minimal-base-and-app-package.md). What builds a Node today is the [Debian packaging module](module-debian-packaging.md) ([decision 0019](decisions/0019-debian-packaging-with-debhelper.md)):

- **The minimal base image** is built by rpi-image-gen (`appliance/rpi_image_gen`, via workflow `.github/workflows/base-image.yml`) from the Debian snapshot pin (`debian-packaging/snapshot.list`). It carries **no** Player application, **no** deployment configuration and **no** release-signing key. It installs one Photo Wall package by name, `photo-wall-node`, from the run's local repo; apt pulls the contexts it pins, `nats-server` and the OS packages it Depends on. OS features (SSH, time, the resolver, device packages, `udev`, which creates the `render` and `input` groups) are rpi-image-gen layers, never a Photo Wall package's Depends.
- **There is no bootstrapper.** Stage 1 (`photo-wall-netboot-init`) locates Central and mounts the base; `photo-wall-node.target` then starts the node units, and the Player and AppManager run from release roots Central stages as squashfs images ([Player architecture](player-architecture.md)). Nothing installs a `.deb` on a Node.

**Deadlines (0014 M5).** Every `uplink` HTTP exchange holds to one absolute deadline end to end: `_Connection.connect` (`uplink/transport.py`) installs a `_DeadlineSocket` after the TLS handshake, so every `recv_into`, `recv` and `sendall` is bounded by what is left of the deadline, not re-armed per read.

**Status:** the base, the packages and the release roots are CI-proven by `base-image`, the `debs` job and the [PID1 scenarios](evidence/player-node-handoff-support/node-lifecycle-qualification.md). Physical Pi boot, HDMI and timing remain bench evidence ([which evidence](../CONTRIBUTING.md#choose-the-right-evidence)).
