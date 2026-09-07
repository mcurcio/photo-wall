# Automated appliance boot qualification

Status: the final-revision harness is implemented for stateless Players and centrally selected releases. Focused portable and PostgreSQL/HTTP tests pass. No current rebuilt-image result is claimed yet.

The [image workflow](../.github/workflows/appliance.yml) builds the exact checked-out revision. The [VM harness](../scripts/test_appliance_e2e.py) consumes its checksum-identified disk, PXE tree, signed accepted root, and separately signed failing candidate. The [boot fixture](module-boot-fixture.md) owns isolated central, PostgreSQL, HTTPS, DNS, NTP, and optional Immich services. Harness code owns lifecycle and evidence only.

## Preflight and isolation

Before starting any service, preflight verifies the source revision, immutable central/worker/builder image IDs, disk checksum, single boot partition, PXE inventory, generic kernel/initramfs hashes, signed accepted and candidate manifests, exact rootfs hashes, common boot ABI, public configuration digest, and candidate fault. Candidate metadata uses `releases.accepted` and `releases.candidate`; no A/B slot names or local update state exist.

Each run creates a private fixture and QEMU `virt` guest. The disk and boot inputs are read-only. Guest writes land in a disposable qcow2 overlay and RAM root. A fixed QEMU UUID supplies generic-VM equipment matching; physical Pi uses its firmware serial. QEMU supplies its documented SBSA watchdog and the generic initramfs proves and preloads the matching Linux driver. The ordinary systemd Player and native GTK/GStreamer renderer must enroll and report. The fixture does not inject a Player, readiness, commitment, observation, health acceptance, release selection, or fallback.

Public reports contain bounded artifact identities, hashes of ticket capabilities, boot/session epochs, allowlisted health states, exact presentation evidence, cleanup status, and explicit qualification flags. They exclude credentials, private keys, raw disk contents, and unbounded service output.

## Required boot sequence

1. Start with an empty central database and no writable Player volume. Central registers the signed accepted release.
2. Boot the accepted release. Bootstrap obtains a central ticket, copies the verified rootfs into RAM, the Player creates fresh credentials, and recognized equipment recovers its centrally assigned Frames. Unknown equipment remains unbound.
3. Restart the Player process and then reboot the VM. Each enrollment receives a higher authority epoch; earlier tokens and grants fail. Any surviving cache file is revalidated before reuse.
4. Stop and restart central while the Player process is running. Current authorized presentation may continue. State delivery resumes after reconnect.
5. Register and stage the signed failing candidate through the authenticated operator API for the exact device. The next boot must carry a trial ticket selected and consumed centrally.
6. Let the stock Player fail sustained health. The production watchdog must request and perform the reboot without fixture intervention.
7. On the next boot, central must select the accepted release. PostgreSQL evidence must show the consumed failed trial, distinct boot attempts, current session binding, and no stale health promotion.
8. Verify the original disk hash is unchanged and clean only resources owned by the run.

Duplicate requests for the same boot ID and request ID must return the same ticket without consuming another trial. A conflicting retry fails. Health from another boot, ticket, release, Player, epoch, or stale time cannot promote a release.

## Native media extension

With the exact production media worker and disposable Immich fixture, the operator helper uses authenticated APIs to create and bind a Frame, commit calibration, configure source/Scene/Program playback, and schedule a photo. It never writes readiness, commits, or observations directly.

Evidence joins the exact worker publication and blob to the secured assignment, offered Plan, current configuration, readiness, valid commit, coordination group, and native `presented` observation. Player, epoch, Plan/revision, assignment, Frame/Output, binding generation, original hash, and prepared JPEG hash must agree.

The cache starts empty. After first presentation, a valid surviving file may be reused only after exact size and digest verification and without a second media request. Deleting or corrupting it must clear readiness and cause reacquisition without reenrollment or a changed central selection. The same checks run after process restart and machine reboot, while recognizing that the cache is disposable and may be absent. Delivery denial tests prove running-process continuity only; they do not promise cold-boot playback without central services.

## Qualification limits

Passing the generic VM qualifies only the exact artifact and emulated environment. It does not qualify Pi EEPROM/PXE, onboard Ethernet, firmware, DRM/HDMI, thermal or resource behavior, replacement handling, or visible multi-display coordination. Those remain physical-bench gates. The complete two-Player/three-Output demo, authenticated browser walkthrough, exact-image native/cache/rollback run, and independent final review must be tied to the final revision before PR #2 leaves draft.
