# GitHub Actions appliance build — 2026-09-05 Pacific

**Latest checkpoint: second hosted ARM64 image build and generic-VM e2e passed.**
See the [second passing artifact and report](#second-hosted-exact-image-boot-pass).
The earlier failures below are retained as historical evidence. This is
integration evidence, not a physical Pi result.

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

Further review found a concrete startup-ordering defect: the required
`ReadWritePaths=/run/photo-wall/player` path was created by an `ExecStartPre`
command, but mount namespaces are prepared before that command can run.
The initial assumption that the command's `+` prefix exempted it from this
check was incorrect. In the pinned systemd 255 implementation,
[`exec_needs_mount_namespace`](https://raw.githubusercontent.com/systemd/systemd/v255/src/core/execute.c)
requires a namespace for nonempty write paths, and
[`apply_mount_namespace`](https://raw.githubusercontent.com/systemd/systemd/v255/src/core/exec-invoke.c)
passes those paths even for fully privileged commands.

The corrected unit uses `RuntimeDirectory=photo-wall/player` and mode `0700`
to create the child before command namespaces. Its root-owned parent and
protected boot report remain separate from Player health. **23 focused tests
passed / 2 Linux file-tooling skips**, and Ubuntu's actual unit validator
passed again. This establishes a source-supported correction; a successful
service execution and exact-image boot remain distinct qualification gates.

The subsequent live check in a disposable, network-isolated Ubuntu ARM64
container running systemd **255.4-1ubuntu8.17** reproduced the old unit's
failure: `Failed to set up mount namespacing: /run/photo-wall/player: No such
file or directory`, followed by `226/NAMESPACE`. The corrected unit passed
the same check as UID 10001: writable runtime and durable state, Wayland Unix
socket connection, read-only user runtime, protected boot record/parent,
hidden home, private temporary files, and runtime-directory removal on stop.
All temporary paths, account and group were removed after both runs. **8
focused preflight tests also passed on Linux**, including actual Unix-socket
cleanup. The first probe exposed a fixture-only redundant group deletion;
that bookkeeping error was corrected before the passing run.

GitHub now runs this short live-systemd preflight immediately after checkout,
before building containers or assembling an image. It uses the checked-in
service sandbox and startup preparation with a synthetic probe executable.
It qualifies that service contract, not the full Player, VM boot, or hardware.

The combined early preflight, RuntimeDirectory correction and bounded APT
transport change passed **833 PostgreSQL-backed tests / 10 host-specific
skips / 4 dependency warnings in 94.18 seconds**. Ruff, all 53 documentation
link sets and actionlint passed. The skipped filesystem tools, GNU tar, TFTP
and root-UID cases remain explicitly separate from the live systemd evidence.
The subsequent group-collision regression passed in the eight-test Linux
preflight suite and the final live probe passed again; no preexisting `wall`
group is reused or removed.

## Warm image confirms the missing runtime path

[Run 34018139184](https://github.com/mcurcio/photo-wall/actions/runs/34018139184)
at feature head `498f7b8`, actual merge/source
`453739ffc95e0a90fde28f22a26b7c70f1b54f23`, completed warm image assembly in
11m11s. Its 5,906,628,608-byte disk SHA-256 was
`c420a33da7dba520c0802c2a38e3b3b9e109330750eec87b92e6c6d32e790e74`.
The [sanitized report](2026-09-05-github-warm-vm-report.json) retained a durable,
fault-free slot A trial boot, then Player `226/NAMESPACE` with classified
`player_runtime` / `ENOENT`. This independently confirms the missing path
reproduced by the live preflight. Enrollment timed out; the protected original
disk stayed unchanged and no qualified image was uploaded. This image predates
the RuntimeDirectory correction.

The [corrected run at 1eb16ef](https://github.com/mcurcio/photo-wall/actions/runs/34019308971)
passed the new live-systemd preflight on GitHub's ARM64 runner at 07:33:39 UTC.
The corrected image and automated generic-VM e2e subsequently passed, as recorded below.

## First hosted exact-image boot pass

[Run 34019308971](https://github.com/mcurcio/photo-wall/actions/runs/34019308971)
completed **successfully** at feature head `1eb16ef`, actual PR merge/source
`61b9950dde9e2d61e149ed7927abe974d306a88a`. The
[sanitized report](2026-09-06-github-passing-vm-report.json) records two
fault-free durable slot A trial boots with distinct boot IDs. The same Player
identity persisted and its authority epoch advanced from 1 to 2. Signed
HTTPS/DNS/NTP, fresh enrollment, power-cycle continuity, central outage/rejoin,
cleanup and original disk integrity all passed. No Player namespace, Python,
OOM or kernel-panic failure was reported. Weston failed in this headless VM;
native rendering and healthy-trial acceptance remain explicitly unqualified.

The [downloadable artifact](https://github.com/mcurcio/photo-wall/actions/runs/34019308971/artifacts/9985713962)
is 1,769,508,410 bytes as a GitHub ZIP, digest
`bb239063bf325a473767a929a471b3ad16a434b69936e0f685cae487f6aaacba`.
Its uncompressed signed disk is **5,906,628,608 bytes**, SHA-256
`9b59a3a9a33810e539f4d1fe30428347ffd97c30c776344a0512bc9fce661100`.
Release ID: `98d49084b3417b2e0cdd08bc5defab08507a220c8930dba4b285312b1ce7ce75`.
The full configuration/rootfs/kernel/initramfs hashes and disposable service
image IDs are in the report. Artifacts use a seven-day retention period and
contain disposable CI trust, not deployment credentials.

The APT configuration change correctly invalidated the extraction cache for
this run: cold assembly took **22m33s**, including 708.539s extraction and
152.826s runtime packages. The new pristine cache was saved for subsequent
revisions. Test setup began at 08:00:12 UTC; fresh VM boot took **9m06s**,
power-cycle boot **9m37s**, and central recovery **36s**. The report completed
at 08:20:27 UTC, and compression/upload completed by 08:24:01 UTC. This is
actual generic ARM64 software-emulated boot evidence, with the documented
kernel/modules/observability substitutions. It does not establish Pi firmware,
fresh LAN PXE, physical HDMI, native healthy-trial acceptance or automatic
rollback. Those report qualifications remain false.

An independent integrated appliance source review found no concrete material
defects after the startup fixes; it did not claim the unexecuted physical and
native acceptance gates. The next revision's hosted run remains separately
tracked and this pass is not relabeled as a pass for later source.

A separate evidence review cross-checked the published ZIP size/hash/link,
source checkout, disk and component hashes, phase durations, both enrollment
records and all qualification fields against the hosted log. No discrepancy
was found.

## Second hosted exact-image boot pass

[Run 34020566014](https://github.com/mcurcio/photo-wall/actions/runs/34020566014)
completed successfully from feature head `d9f656b3730b799bff89d4103f5c87774c665c35`
through actual PR merge/source revision
`64f9558f3598b8bf8424c6888f794d92737aed1c`. The signed disk was built,
reopened and then booted as the exact uploaded image. It is 5,906,628,608 bytes
with SHA-256
`d55fb36185b42e0f57203bd897a2f14d3c96d1546479bdfbf47cebf469db3933`.
Its release ID is
`20e30c90a28f4e89d42c91f058afb12f98f8fbe0d01794ddacb6d3d2fa978fe3`;
rootfs, configuration, generic kernel and generic initramfs hashes are
`c766c86cac7c053d2486828e5fda009b596f233a8518d22c948c3bb7f1bfaf3a`,
`87bb0ffdc836a363418a779c875a4def48d2e462b06a3f4804833b203e5e595a`,
`552e27f48eefa445d0d6bbc501e5972d1a43bf0e2cb78ea5345fed9e01736f58` and
`a77245f7217ca4c24266eab10d0a2d2a1e4b2a321d2fa5cda9cb90572a5c9e09`.
The boot ABI hash is
`f3bdff50bb831bba08a53718b450799537fe2c630ef19cc886f12398395693d2`.

The hosted build reused the verified ARM64 extracted-base cache. Its recorded
assembly phases were 7.879s cache restore, 151.956s runtime packages, 1.094s
Player package, 0.039s source export, 190.185s image preparation, 221.464s
finalization and 34.892s generic initramfs generation: **10m13s** from
assembly start to completed metadata, versus the recorded **22m33s** cold
assembly baseline (about 55% less elapsed time). The builder image was
`sha256:5730240d163aba338b8f0333001619c46b6bee94cf2c32c78a95d4e36850a5bc`
and the central image was
`sha256:9a2108aa020b82539fb4bedbdaf271a37ea08dc4366de81b32c2245e0cbb3919`.

The public [signed image artifact](https://github.com/mcurcio/photo-wall/actions/runs/34020566014/artifacts/9986238336)
is `photo-wall-arm64-64f9558f3598b8bf8424c6888f794d92737aed1c.zip`,
artifact ID `9986238336`, 1,769,515,428 bytes, SHA-256
`f0de74c3135f1d646d9c7c63352370f25aeafd6c4439a0e7335832d5411e77a4`.
The [e2e report artifact](https://github.com/mcurcio/photo-wall/actions/runs/34020566014/artifacts/9986238598)
is ID `9986238598`, 1,521 bytes, SHA-256
`59f408fbb125edeb1af0662fe628ec806f748ebd644d46ca343a726baf9d9a66`.

The sanitized report records all five generic-VM checks passed: fresh durable
enrollment, central outage/rejoin, identity across the power cycle, signed
HTTPS/DNS/NTP checks and unchanged original-disk integrity. Both durable slot A
trial boots used the same release and distinct boot IDs
`5d28225f-a417-4be7-a937-c2cb05caa512` and
`548f3c33-2276-4006-9fd7-6bb85f9636a8`; the same Player ID retained identity
while its authority epoch advanced from 1 to 2. The report's source and Player
inventory are bound to merge revision `64f9558f...`; the Player wheel is
`photo_wall_player-0.1.0+g64f9558f3598b8bf8424c6888f794d92737aed1c-py3-none-any.whl`,
SHA-256
`d93cced129ee2b81e31645011cc016bb3d3ca919976d12c92467be51a56baf6f`,
47,685 bytes.

This is generic ARM64 software-emulated boot evidence. Weston exited with
status 1 in the headless guest, so healthy-trial acceptance, native rendering,
physical Pi/PXE, HDMI and automatic rollback remain false. The exact source,
component identities, substitutions and qualification fields are retained in
the [public e2e report artifact](https://github.com/mcurcio/photo-wall/actions/runs/34020566014/artifacts/9986238598).
