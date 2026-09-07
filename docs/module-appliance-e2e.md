# Automated appliance boot qualification

Status: the appliance CI gate is intentionally narrow: build the signed image, boot it with the generic ARM64 kernel, and prove the production Player enrolls. Software behavior runs in the separate `Controller and Player software e2e` workflow. The harness retains an explicit `--scope full` mode for final exact-image media, reboot, and rollback qualification, but that longer acceptance run is not the routine appliance build gate.

The [image workflow](../.github/workflows/appliance.yml) builds the exact checked-out revision. The [VM harness](../scripts/test_appliance_e2e.py) consumes its checksum-identified disk, PXE tree, and signed accepted root. The [boot fixture](module-boot-fixture.md) owns isolated central, PostgreSQL, HTTPS, DNS, and NTP services. Harness code owns lifecycle and evidence only.

## Preflight and isolation

Before starting any service, preflight verifies the source revision, immutable central/builder image IDs, disk checksum, single boot partition, PXE inventory, generic kernel/initramfs hashes, signed release inputs, exact rootfs hashes, boot ABI, and public configuration digest. The build still validates its generated failing candidate, while the smoke scope does not stage or boot that candidate.

Each run creates a private fixture and QEMU `virt` guest. The disk and boot inputs are read-only. Guest writes land in a disposable qcow2 overlay and RAM root. A fixed QEMU UUID supplies generic-VM equipment matching; physical Pi uses its firmware serial. QEMU supplies its supported PCI `i6300esb` watchdog and the generic initramfs proves and preloads the matching Linux driver. The ordinary systemd Player and native GTK/GStreamer renderer must enroll and report. The fixture does not inject a Player, readiness, commitment, observation, health acceptance, release selection, or fallback.

Public smoke reports contain bounded artifact identities, hashes of ticket capabilities, the observed boot/session epoch, cleanup status, and explicit qualification flags. They exclude credentials, private keys, raw disk contents, and unbounded service output.

## Routine appliance smoke sequence

1. Start with an empty central database and no writable Player volume. Central registers the signed accepted release.
2. Boot the accepted release. Bootstrap obtains a central ticket and copies the verified rootfs into RAM.
3. Prove the production Player creates fresh credentials, enrolls, and reports volatile persistence and the selected accepted release.
4. Verify the original disk hash is unchanged and clean only resources owned by the run.

Duplicate boot requests, stale health, session replacement, cache recovery, controller outages, media delivery, and execution behavior are covered by software integration and focused transaction tests. The longer `--scope full` image run remains available when collecting exact-image reboot, native media, and central rollback evidence.

## Native media extension

With the exact production media worker and disposable Immich fixture, the operator helper uses authenticated APIs to create and bind a Frame, commit calibration, configure source/Scene/Program playback, and schedule a photo. It never writes readiness, commits, or observations directly.

Evidence joins the exact worker publication and blob to the secured assignment, offered Plan, current configuration, readiness, valid commit, coordination group, and native `presented` observation. Player, epoch, Plan/revision, assignment, Frame/Output, binding generation, original hash, and prepared JPEG hash must agree.

The cache starts empty. After first presentation, a valid surviving file may be reused only after exact size and digest verification and without a second media request. Deleting or corrupting it must clear readiness and cause reacquisition without reenrollment or a changed central selection. The same checks run after process restart and machine reboot, while recognizing that the cache is disposable and may be absent. Delivery denial tests prove running-process continuity only; they do not promise cold-boot playback without central services.

## Qualification limits

Passing the generic VM qualifies only the exact artifact and emulated environment. It does not qualify Pi EEPROM/PXE, onboard Ethernet, firmware, DRM/HDMI, thermal or resource behavior, replacement handling, or visible multi-display coordination. Those remain physical-bench gates. The complete two-Player/three-Output demo, authenticated browser walkthrough, exact-image native/cache/rollback run, and independent final review must be tied to the final revision before PR #2 leaves draft.
