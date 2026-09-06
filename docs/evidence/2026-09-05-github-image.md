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
