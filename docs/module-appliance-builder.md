# Appliance builder and RAM-root bootstrap

Status: the builder and stateless bootstrap are implemented. Their portable and PostgreSQL-backed contracts pass; the exact rebuilt image, generic ARM VM, and physical Pi PXE/HDMI gates still require execution on the final revision. This document follows [decision 0006](decisions/0006-central-authority-and-stateless-players.md) and supersedes the former `PWSTATE` and local A/B design.

**This document describes the signed, combined base+app image (0008 D1) — still the as-built netboot mechanism.** [Decision 0009](decisions/0009-minimal-base-and-app-package.md) supersedes it with a minimal, unsigned base image plus the Player as a downloadable `.deb`; that replacement's own build and boot-time bootstrapper are described in [The 0009 minimal base and bootstrapper](#the-0009-minimal-base-and-bootstrapper-in-progress) below. The PXE boot chain that would run the 0009 replacement is not yet wired, so everything in this document remains the mechanism that actually boots a netboot Player today.

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

## The 0009 minimal base and bootstrapper (in progress)

[Decision 0009](decisions/0009-minimal-base-and-app-package.md) replaces the signed, combined image above with two pieces, built and unit-tested independently of everything else on this page:

- **The minimal base image** (`scripts/build_ci_base_image.py` → `appliance.build.configure_root_base`) squashes a root that carries **no** Player application, **no** deployment configuration, and **no** release-signing key — the owner's UX-over-security ruling drops app authenticity to a plain corruption-check sha256 the bootstrapper itself verifies, so no signing key exists anywhere in this image. It installs only the bootstrapper's minimal import closure (`appliance/provision.py` plus `player/mdns_discovery.py` — never `player.service`, which pulls in GTK/GStreamer), `zeroconf`/`ifaddr` as native packages, and the `photo-wall-provision.service` systemd unit that starts it after networking comes up.
- **The bootstrapper** (`appliance/provision.py`) is what that unit runs: it discovers central by mDNS (bounded retry; a base with no central on the LAN keeps trying), fetches `GET /v1/app/manifest`, downloads `GET /v1/app/package/{sha256}.deb`, verifies the bytes against the manifest sha256 (a corruption check only — a mismatch discards and retries, it is never installed), `dpkg -i`s it into the running RAM root, writes the resolved origin forward as the app's explicit `central_origin` (`/etc/photo-wall/public.json`, setting `allow_http: true` when the discovered origin is plain HTTP) so the Player's own `resolve_origin` does not perform a second, independently-resolved mDNS browse, and starts `photo-wall-player.service`. It never enrolls — the app enrolls by serial, once, after it starts.

**Status: pieces built and tested in isolation; the boot chain that would run them is not yet wired.** `tests/test_provision.py` exercises the bootstrapper end to end against faked mDNS/HTTP/`dpkg`/`systemctl`; `scripts/build_ci_base_image.py`'s own output manifest records `{"rootfs_built": true, "boot_tree_staged": true, "netboot_boot_chain_wired": false, "physical_pi": false}` — it deliberately does not wire a working, ticket-free PXE boot chain. Today's initramfs (described earlier on this page) still fetches a signed `BootTicket`/`Release` before it will mount anything at all; retiring that ticket/signature protocol so it can hand off to this bootstrapper instead is its own, separate, deferred slice. Until that lands, netbooting a Pi against this minimal base image is not possible — the mechanism described in the rest of this document is what actually boots.
