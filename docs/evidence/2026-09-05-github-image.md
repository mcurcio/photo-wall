# GitHub Actions appliance build — 2026-09-05 Pacific

**Actual hosted ARM64 build passed; automated boot qualification failed before
starting the VM.** This is integration evidence, not a physical Pi result.

[Run 34012404107](https://github.com/mcurcio/photo-wall/actions/runs/34012404107)
built feature head `205d222f296b190b42b6c2a8d5b6e21cd7ba259f` through GitHub's PR
merge checkout `8e10a9b98a04a70c06f6b2a3d4d9b24a046b2f0e`. The merge checkout is
the actual appliance and Player source revision. The runner was
`ubuntu-24.04-arm`; Python 3.12.3 and Packaging 26.3 built the Player bundle.
Exact public identities are in the [machine-readable record](2026-09-05-github-image.json).

The signed Ubuntu download, root extraction, pinned package acquisition,
Player-only package, signed image finalization/reopening and generic test
initramfs all completed successfully. The finalized disk was 5,906,628,608 bytes
with SHA-256
`199b1365361212e38dd77efac7f12eb7d0931093f4ce1487c5a2990119292e7c`.

Build-stage durations from the hosted log:

| Stage | Elapsed |
|---|---:|
| Signed base fetch | 11 seconds |
| Decompression | 36 seconds |
| Root extraction | 12 minutes 22 seconds |
| Package acquisition | 5 minutes 5 seconds |
| Image preparation | 2 minutes 46 seconds |
| Finalization and reopening | 3 minutes 22 seconds |
| Generic initramfs | 33 seconds |

Initial free space measured 113,677,852,672 bytes. Neither runner disk exhaustion
nor GitHub authentication caused the failure.

The e2e step rejected `generic-boot/manifest.json` with `invalid_regular_file`:
the harness used the fixture JSON reader's 1 MiB bound for a full kernel-module
inventory. An existing real generic manifest measured 3,365,677 bytes, confirming
that this artifact class exceeds that bound. The correction gives the producer
and consumer a shared, explicit manifest limit and records preflight failures
in the public e2e report. A new hosted run must validate the corrected handoff.

This run never started a VM, enrolled a Player or passed reboot/recovery checks.
Image upload is gated on successful e2e, so this disk was **not uploaded** and is
not available as a downloadable qualified artifact. Only the public build-log
artifact was uploaded. No healthy-trial, automatic rollback, physical Pi/PXE,
HDMI or visible-rendering result is claimed. See the [e2e contract](../module-appliance-e2e.md).

## Follow-up source verification

At `1edbbd358f8591e9fda1c39e5837a43cfb38bf27`, the full local PostgreSQL-backed
suite passed **802 tests / 4 explicit Linux-only skips / 4 dependency warnings**.
The skips were two opt-in Linux image-tooling fixtures, real Linux TFTP and the
root-owned Linux UID gate. The sandboxed portable run had 683 passes, 122
integration skips and one loopback permission failure; the authorized full
integration run passed that loopback test.

[Standard GitHub CI job](https://github.com/mcurcio/photo-wall/actions/runs/34014002087/job/101434552008)
passed **756 tests / 50 explicit skips**, then **55 pinned Linux media preparation
tests**. A separate copy of the exact committed source in the existing isolated
ARM64 builder passed **153 tests in 77.84 seconds** with actual FAT/ext4/squashfs
creation, libguestfs metadata-preserving extraction and root-owned updater
permissions. This was focused file tooling, not another manual Pi image build.

An independent startup review subsequently found that ordinary accepted boots
incorrectly failed the enabled trial-acceptance service. The correction makes
a protected, current non-trial report a successful no-op only when it matches
the accepted selected and active slot. Trial acceptance remains strict. The
follow-up change passed **132 related local checks / 1 Linux UID skip**, then
**76 actual Linux updater checks**, including accepted-boot CLI success without
health polling/state mutation and rejection of mismatched or malformed reports.
This establishes the command's behavior, not an actual systemd or Pi reboot.
Counts overlap and are not summed.

## Hosted VM attempt after the manifest fix

[Run 34014002058](https://github.com/mcurcio/photo-wall/actions/runs/34014002058)
also built and reopened its signed disk successfully. Actual PR merge/source
revision: `6340b92066f0a86b2034f29ee1b29bbf8bdf638d`, from feature head `1edbbd3`.
Disk size: 5,906,628,608 bytes; SHA-256:
`16a50d54cfc0613209c23d6711938f5b64276abd0237729a79452fd373e41881`.

The [public e2e report](2026-09-05-github-vm-report.json) records a successful
signed HTTPS/DNS/NTP fixture probe, followed by `guest_enrollment_timeout` after
ten minutes. The guest container remained running. Diagnostics identified
failed Player, Weston and trial-acceptance services, with no observed kernel
panic, out-of-memory event, CHDIR failure or allowlisted Python exception type.
The available report does not establish the service failure cause.

Cleanup passed and the original disk hash remained unchanged. No reboot,
reconnection or generic/physical qualification passed, and no image artifact
was uploaded. A subsequent harness correction retains validated boot reports
when they leave the bounded serial-log tail, and records numeric service exit
codes plus final inventory count. **20 focused e2e tests passed**, including
late enrollment after log rollover and sanitized exit-code reporting. That is
harness evidence, not a successful rerun of the guest.

## Cached-build boot diagnostics

[Run 34015771816](https://github.com/mcurcio/photo-wall/actions/runs/34015771816)
completed another signed image build and populated the pristine Ubuntu cache.
Its actual PR merge/source revision was
`ce30e4364e54af9eb2fbb5b6ddac9514afbf8220` (feature head `6a101a9`).
The 5,906,628,608-byte disk SHA-256 was
`2e29a6ee9390cdb36d773e35f1c87df03997143f656bf63e89c626140aaab9ec`.
The [retained sanitized report](2026-09-05-github-cached-vm-report.json) records
a durable, fault-free trial boot of slot A and the expected release, followed
by zero enrolled Players and `guest_enrollment_timeout`. Player exited with
**226/NAMESPACE**; Weston exited 1/FAILURE and trial acceptance was terminated
with signal 15. The original disk remained unchanged; no image was uploaded
and no boot/reboot qualification passed.

This evidence narrows the Player failure to systemd namespace setup. A separate
service review found a definite compositor-access contradiction:
`ProtectHome=yes` hid `/run/user`, including the configured Wayland socket.
The correction makes `/run/user` visible read-only while keeping `/home` and
`/root` inaccessible, with the existing write exceptions retained. It does
**not** establish the exact cause of 226/NAMESPACE. New diagnostics classify
allowlisted mount paths and errno messages so a future failure can be located
without publishing arbitrary guest paths or messages.

The combined correction and diagnostics passed **45 focused tests / 2 Linux
file-tooling skips**, then **824 PostgreSQL-backed tests / 10 host-specific
skips / 4 dependency warnings in 93.62 seconds**. Ubuntu's actual
`systemd-analyze --man=no verify` passed for the updated service inside the
retained isolated Linux root; this checks unit configuration and executable
paths, not successful namespace creation or socket access. No manual Pi image
was rebuilt. A hosted boot of the corrected revision remains required.
