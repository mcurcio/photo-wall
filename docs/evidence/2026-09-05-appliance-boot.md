# First appliance VM attempt — 2026-09-05

Status: the exact signed rootfs mounted successfully with durable state, but operating-system services failed. This is a failed qualification attempt, not a runnable-appliance or physical Pi pass. The [built image record](2026-09-05-appliance-image.md) owns the artifact and package identities.

## Inputs and substitutions

The VM read the finalized 5,906,628,608-byte disk from revision `073f57d7547507de452a2fd273088d7542a6d070` through a read-only Docker volume. Its SHA-256 is `d822bc819606c63a9fdcf40b03d6a65933d7881326757de0b8b2a34ae94b3448`. A new qcow2 overlay received all guest writes; the original disk remained unchanged.

The generic ARM64 test uses QEMU `virt`, a Cortex-A72 CPU model, two virtual CPUs and 3 GiB guest RAM within a 4 GiB/two-CPU container. It has virtio block/network devices, user-mode DHCP/DNS, serial output, no host ports or devices, and a ten-minute execution bound. The unused NIC boot ROM is disabled for this direct-kernel path. Logs are capped at one 10 MiB file with compression disabled. It connects only to the task's internal HTTPS/DNS/NTP fixture network.

The production Pi initramfs input was 64,614,282 bytes, SHA-256 `98435d9d6d785aea06676ceb9319284e61baeff99a78b1ef253006d9ff6d2d8e`. The [test builder](../../scripts/build_vm_initrd.py) preserves its raw early cpio and gzip main layout and compares the original userspace bytes after reopening. It substitutes the generic kernel/module tree, explicitly loads the required virtio/filesystem drivers, normalizes the known root `/lib` link and adds a serial-only boot-report hook to `init-bottom/ORDER`. The production Python/bootstrap/public configuration remains byte-identical. The resulting test artifacts were:

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| Generic kernel `Image` | 59,079,048 | `552e27f48eefa445d0d6bbc501e5972d1a43bf0e2cb78ea5345fed9e01736f58` |
| Test initramfs v2 | 177,054,079 | `7603ae75f8b69e2b6ab4f00b51fc47abf88f9d30ca91c8ef22f649ae5f0396e3` |
| Test manifest | — | `0924142ca964faced8d6db3d0a9a77dd589e27b301d803bce6689af96067eee3` |

Generic kernel release `6.8.0-139-generic` replaces Pi release `6.8.0-1047-raspi` only in this test initramfs/kernel pair. This cannot establish Pi firmware, EEPROM/PXE, Ethernet hardware or HDMI behavior.

## Observed result

The guest started initramfs-tools, discovered its virtio network interface and obtained `10.0.2.15` through QEMU's DHCP service. It authenticated/copied the common release, created the trial slot and mounted the signed SquashFS through OverlayFS. The serial hook emitted this protected bootstrap report:

```json
{"boot_id":"098f65ce-2257-4002-b2c9-2ec0e50a7e8c","fault":null,"persistence":"durable","release_id":"f8322e1329a1dc8e11f66f3c31bb4fa074f9d1638c4dd82b9e8b925bfa78bb60","schema":1,"slot":"A","trial":true}
```

After switching into the root, systemd started but `systemd-networkd` and `systemd-resolved` repeatedly failed with `200/CHDIR` and journal-socket permission errors. The VM was stopped and its overlay and serial log retained outside Git. No central enrollment, healthy trial acceptance, successful reboot or rollback is claimed for this attempt. Inspection confirmed the signed lower root was correctly owned with mode `0755`; the runtime OverlayFS upper root had mode `0700`, which denied traversal to non-root services. The source correction makes the upper and merged root `0755`, verifies root ownership and preserves reverse cleanup on failure. GitHub Actions must build and boot a new artifact before this correction is considered qualified; this historical image does not contain it.

Earlier container setup failures were resolved before this guest boot: incompatible single-file log compression, a launcher copied with inaccessible ownership/mode, and an absent optional NIC ROM. These failures did not execute the appliance or change the signed disk. The first generated test hook also required correction because initramfs-tools sources `ORDER` rather than discovering new hooks automatically.

Private evidence is retained at `/private/tmp/photo-wall-vm-073f57d-a.serial.log` and `/private/tmp/photo-wall-vm-073f57d-a.json`; the failed overlay remains in the task-owned `pw-vm-073f57d-20260905-a-state` volume. Signing keys, identity material and image binaries are excluded from Git.
