# Automated appliance boot qualification

Status: the appliance CI gate is intentionally narrow: build the signed image, boot it with the generic ARM64 kernel, and prove the production Player enrolls. Software behavior runs in the separate `Controller and Player software e2e` workflow. Push and pull-request events run the appliance `smoke` scope. A manual workflow dispatch can select `full`; the default remains `smoke`. The full path conditionally builds the production ARM64 media worker and binds its immutable image ID into both the image manifest and harness invocation. A missing worker fails preflight instead of silently skipping native media.

The [image workflow](../.github/workflows/appliance.yml) builds the exact checked-out revision. The [VM harness](../scripts/test_appliance_e2e.py) consumes its checksum-identified disk, PXE tree, and signed accepted root. The [boot fixture](module-boot-fixture.md) owns isolated central, PostgreSQL, HTTPS, DNS, and NTP services. Harness code owns lifecycle and evidence only.

## Preflight and isolation

Before starting any service, preflight verifies the source revision, immutable central/builder image IDs, disk checksum, single boot partition, PXE inventory, generic kernel/initramfs hashes, signed release inputs, exact rootfs hashes, boot ABI, and public configuration digest. The build still validates its generated failing candidate, while the smoke scope does not stage or boot that candidate.

Each run creates a private fixture and QEMU `virt` guest. The disk and boot inputs are read-only. Guest writes land in a disposable qcow2 overlay and RAM root. A fixed QEMU UUID supplies generic-VM equipment matching; physical Pi uses its firmware serial. QEMU supplies its supported PCI `i6300esb` watchdog and the generic initramfs proves and preloads the matching Linux driver. The ordinary systemd Player and native GTK/GStreamer renderer must enroll and report. The fixture does not inject a Player, readiness, commitment, observation, health acceptance, release selection, or fallback.

Public smoke reports contain bounded artifact identities, hashes of ticket capabilities, the observed boot/session epoch, cleanup status, and explicit qualification flags. They exclude credentials, private keys, raw disk contents, and unbounded service output.

## Enrollment observation boundary

The authenticated inventory probe validates the complete [Installation contract](architecture.md#installation-inventory-and-enrollment-observation), including nested Outputs and Frames, before projecting Equipment session facts. Successful transport with an empty inventory is a valid `pending` observation. Health may still be empty and Output feedback may arrive later; neither is required to recognize enrollment. Malformed inventory remains a contract failure.

The harness waits explicitly for `ready` and a non-null session matching the boot's Equipment. On restart the same Player at an unchanged or older epoch remains pending until a strictly higher epoch appears. A retired Player, different Equipment, or unexpected additional Player fails the isolated-fixture check. It never uses the observation object's truthiness as completion. Boot selection, enrollment, release-health acceptance, and presentation are observed separately. Evidence converts typed models to JSON primitives at the report boundary, including pending observations, so ordinary JSON report writing supports both states while waiting for convergence.

The [release probe](../scripts/vm_release_probe.py) and host consume the same [strict release-evidence contract](../scripts/vm_release_contract.py), pinned into the fixture helper closure. Both validate versioned evidence and staging results, required nullable fields, exact types and bounds, and reject unknown fields. The evidence query is read-only: a candidate's consumed trial must retain the hash of that exact boot ticket. A failed boot status alone cannot establish consumption. Rollback separately records the failed, noncurrent candidate attempt and the current, nontrial accepted fallback, retaining the centrally selected candidate and accepted release identities. The fallback must match the restored Equipment, Player, and current authority epoch. Reports contain ticket hashes and separate central trial/fallback evidence; they expose no ticket capability and infer no historical session from the Equipment's current session fields.

## Routine appliance smoke sequence

1. Start with an empty central database and no writable Player volume. Central registers the signed accepted release.
2. Boot the accepted release. Bootstrap obtains a central ticket and copies the verified rootfs into RAM.
3. Prove the production Player creates fresh credentials and enrolls with a session matching the selected boot. Record volatile persistence and the selected release from their own boot/report fields; enrollment alone does not establish sustained health or rendering.
4. Verify the original disk hash is unchanged and clean only resources owned by the run.

Duplicate boot requests, stale health, session replacement, cache recovery, controller outages, media delivery, and execution behavior are covered by software integration and focused transaction tests. Dispatch `ARM appliance build and launch` with `scope: full` when collecting exact-image reboot, native media, and central rollback evidence. Assembly and execution remain in one hosted job because the manifest references the uncompressed disk, generic boot tree, signed bundles, private deployment inputs, and rollback candidate in that job's temporary directory.

## Native media extension

With the exact production media worker and disposable Immich fixture, the operator helper uses authenticated APIs to create and bind a Frame, commit calibration, configure source/Scene/Program playback, and schedule a photo. It never writes readiness, commits, or observations directly.

Evidence joins the exact worker publication and blob to the secured assignment, offered Plan, current configuration, readiness, valid commit, coordination group, and native `presented` observation. Player, epoch, Plan/revision, assignment, Frame/Output, binding generation, original hash, and prepared JPEG hash must agree.

The full hosted path starts with an empty RAM cache and proves a native presentation. A constrained test-only guest service then restarts the production Player with the valid file intact, after deleting its content-addressed file, and after corrupting its first byte. Every restart must create a higher authority epoch and make the earlier session unable to receive grants. The valid survivor must be verified and reused without another media GET; deletion and corruption must cause an authenticated reacquisition. Every presentation must retain the exact variant and original hashes, Frame, and Output from the centrally secured selection. A later machine restart proves the RAM cache is gone and reacquired, and centrally selected rollback proves the same again. Delivery denial proves the Player cannot reach the upstream source; it does not promise cold-boot playback without central services.

## Qualification limits

Passing the generic VM qualifies only the exact artifact and emulated environment. It does not qualify Pi EEPROM/PXE, onboard Ethernet, firmware, DRM/HDMI, thermal or resource behavior, replacement handling, or visible multi-display coordination. Those remain physical-bench gates. The complete two-Player/three-Output demo, authenticated browser walkthrough, full exact-image run, and independent final review must be tied to the final revision before PR #2 leaves draft.
