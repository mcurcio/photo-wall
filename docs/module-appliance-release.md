# Central signed release and rollback contract

Status: central release authority, stateless bootstrap, and volatile trial watchdog are implemented and tested in isolation. Complete built-image rollback and physical Pi boot qualification remain pending. This contract supersedes the former Player-local A/B, `SlotStore`, and `PWSTATE` design. Earlier evidence for that design remains historical evidence only.

## Signed artifact boundary

The common bootstrap, central release authority, and build tooling share the standard-library `contracts.release.Release` format. Canonical manifest bytes are sorted compact UTF-8 JSON with one trailing newline and exactly five fields: `schema=1`, a clean 40-character Git `revision`, SHA-256 `boot_abi`, `rootfs_sha256`, and bounded integer `rootfs_size`. The public artifact name is derived as `rootfs-<sha256>.squashfs`; manifests cannot choose paths, commands, or URLs. Release ID is the SHA-256 of canonical manifest bytes. A detached Ed25519 signature covers those exact bytes. (0008 decision 4 re-froze this from a prior six-field shape that also bound a `configuration_sha256` digest of the boot-tree config files into the signed release; a six-field manifest is rejected on decode, not coerced.)

The selected bootstrap/kernel/module/firmware set defines the fixed boot ABI. Both central registration and bootstrap verification reject another boot ABI. The `configuration_sha256` binding is removed: deployment config (`release_origin`, `central_origin`, `ca.pem`) is no longer hashed into a separate manifest field, so those files are operator-editable on the boot tree without re-signing. Only the rootfs bytes and `release.pub.pem` are cryptographically bound. Note that the **netboot rootfs still embeds a copy of these config files today** (`configure_root` writes them into the root that is squashed and hashed), so a netboot rootfs is not yet byte-identical across deployments with different origins/CA — the D0 flash image is the fully generic path (it bakes only `release.pub.pem`), and making the netboot rootfs equally generic (config injected at boot from the tree rather than baked) is a further step. The bootstrap verifies signature before interpreting the manifest, then verifies the downloaded rootfs length and SHA-256 before mounting it. The same verified bytes are copied into RAM, mounted read-only, and given a bounded tmpfs overlay. Running userspace and rollback require no local image slots.

## Central authority and durable records

PostgreSQL is the sole durable release authority. It stores:

- immutable signed release metadata;
- the operator-selected default accepted release;
- each centrally recognized equipment record's accepted and optional candidate release;
- boot attempts and their current status;
- consumed equipment/release trials;
- the Player/session epoch bound to the current boot ticket.

Registering a release verifies its signature and compatibility before insertion. A release ID cannot later name different manifest or signature bytes. The operator sets the default only to a registered release. Newly observed equipment starts from that default; existing equipment retains its own accepted release and explicit candidate.

Staging a candidate is an explicit central operation. Staging the already accepted release is a no-op. A device cannot stage another unconsumed candidate over the current one, and a device/release trial can be consumed only once.

## Boot selection and enrollment binding

At each boot the common bootstrap derives a bounded equipment observation, obtains disciplined time on the trusted provisioning LAN, and sends `device_id`, Linux `boot_id`, and a random `request_id` to central. Central serializes selection for that equipment and returns a `BootTicket` containing the exact signed release metadata.

Selection is idempotent for the same equipment, boot, and request tuple. A conflicting reuse fails closed. If an unconsumed candidate exists, central inserts the boot attempt and consumed-trial record in the same PostgreSQL transaction before returning the candidate ticket. Otherwise it selects the equipment's accepted release. Issuing a newer boot attempt supersedes an earlier ordinary attempt and marks an unaccepted earlier trial failed.

The bootstrap verifies that ticket, request, equipment, boot, manifest, release ID, signature, boot-ABI compatibility, rootfs length, and digest all agree. Before it downloads candidate bytes, trusted initramfs code must open a kernel watchdog and set its bounded timeout. Candidate boot fails closed when no watchdog device is available. The bootstrap then writes a protected RAM-only `/run/photo-wall/boot.json` for the selected userspace and mounts the verified root. Nothing from this selection survives locally across reboot.

Enrollment proves a fresh process key together with the ticket, equipment observation, and Linux boot. In the enrollment transaction central binds the current boot attempt to the resulting Player ID and new authority epoch. A stale ticket or an earlier session cannot report health or regain execution authority.

## Promotion and automatic rollback

While a selected release remains unaccepted, the Player sends health to `/v1/player/boot-health`. Central checks all of the following inside its transaction:

- the Player exists, is not retired, and still owns the supplied authority epoch;
- the ticket is the equipment's current boot attempt;
- the boot attempt remains in a health-eligible state;
- the equipment row is still bound to that Player/session;
- the observation is fresh, ordered, and continuously healthy within the configured sample-gap bound.

The default promotion interval is 30 seconds and health samples may be at most two seconds old. A stale, duplicate, unhealthy, wrong-ticket, wrong-boot, or wrong-session report cannot promote a release. Once continuous health meets the interval, central marks the attempt healthy. For a candidate trial it atomically changes the equipment's accepted release and clears the candidate.

Trusted bootstrap arms the hardware watchdog before entering a candidate root. Watchdog drivers are fixed to nowayout mode by the boot command line; systemd takes over with a 30-second runtime watchdog. This covers bootstrap-to-userspace handoff and userspace-manager failure. Signature, download, verification, or mount failure returns to the initramfs hook, which requests an immediate reboot.

`appliance.updates.TrialWatchdog` is deliberately volatile. It reads the protected current boot report and the Player's current health file, which includes central's release-acceptance acknowledgment. A nontrial boot needs no trial-watchdog action. If a trial is acknowledged as accepted, the trial watchdog finishes while systemd continues the ordinary machine watchdog. If the trial timeout expires first, it records a bounded RAM-only reboot request and fails its systemd unit; the service's `OnFailure` path owns the actual reboot. The watchdog never chooses a release and never keeps a rollback record.

On the next PXE boot, central sees that the candidate trial was already consumed. If it was not promoted, the equipment's accepted release remains unchanged and is selected automatically. Rollback therefore means a real reboot followed by a fresh centrally selected, reverified, RAM-loaded root and a fresh Player session. It never restores old credentials, plans, commitments, cache pins, or execution journal state.

## Failure behavior

Missing central release policy, trusted time, ticket service, rootfs service, or verification inputs fails cold boot closed. There is no offline boot fallback or device-local accepted-release copy. A failed central transaction cannot leak a candidate ticket whose trial was not consumed. Retrying a successfully committed request returns the same ticket. Central restart preserves release, boot-attempt, and trial state in PostgreSQL.

An optional Player cache is outside this release protocol. It may reuse independently verified media files after the fresh session reconciles assignments, but its presence, absence, or corruption cannot influence release selection or promotion.

The fixed boot ABI restriction remains. Updating the kernel, firmware, EEPROM, or bootstrap requires a separately qualified verified-boot fallback design.

## Interfaces and operational configuration

`central.releases.ReleaseAuthority` owns release registration, default policy, per-device staging, boot selection, enrollment/session binding, health evaluation, promotion, and inventory. Central exposes the ticket, rootfs, and authenticated health operations through its application API when release configuration is present.

Operator registration and staging share the required `ReleaseRegistrationReceipt` (`release_id` digest) and `ReleaseStagingReceipt` (strict boolean `staged`) models in `central.release_models`. The observer rejects missing, extra, or incorrectly typed receipt fields, requires the registered identity to match its signed manifest, and accepts staging success only when `staged` is true; the API preserves false for the legitimate already-accepted no-op. Signature verification, operator authentication, and the existing 201/200 response statuses are unchanged. The [ordinary staging integration regression](../tests/test_vm_release_stage_integration.py) exercises the helper through real Central handlers and PostgreSQL, including authentication errors and malformed receipts; the pinned fixture source closure explicitly copies the exact receipt module into derived images, including cached-base builds.

`appliance.bootstrap.boot` is the stateless initramfs operation. Its public configuration contains the central release origin, controlled time server, fixed boot ABI, and Ed25519 public key; deployment config (origins, CA) is not bound into that boot ABI. `appliance.updates.verify_release` is the bounded OpenSSL verifier shared by bootstrap tests; the remainder of that module contains only the volatile watchdog and its protected reboot-request adapter. There is no staging, selecting, marking-good, or local rollback CLI.

The central database migration is `012_release_authority.sql`. Release rootfs bytes remain authoritative central blobs; PostgreSQL stores metadata and lifecycle records rather than the image contents.

## Evidence and remaining acceptance

Unit and PostgreSQL tests cover canonical release parsing/signatures, immutable registration, accepted/candidate policy, transactionally consumed trials, idempotent and conflicting boot requests, stale tickets/sessions/health, continuous-health promotion, central restart state, stateless bootstrap verification/copy/mount seams, mandatory candidate watchdog arming, and watchdog timeout/acceptance behavior. Dated local-slot and systemd-adapter evidence under [the evidence index](evidence/README.md) exercised useful signing, health, and failure seams but predates this central design and does not qualify it end to end.

Acceptance still requires the exact built image to boot a candidate, fail health, invoke the real reboot path, receive the centrally accepted release on the next PXE boot, and re-enroll without local state. Physical Pi/PXE, power interruption, dual HDMI, and native sustained-health behavior remain separate gates in [validation](validation.md).
