# Automated appliance boot qualification

The [image workflow](../.github/workflows/appliance.yml) builds the Pi disk in
GitHub Actions. The [VM harness](../scripts/test_appliance_e2e.py) consumes that
job's exact checksum-identified artifact. Passing focused harness tests alone
does not establish a successful image build or boot.

The harness owns only test lifecycle and evidence. The existing appliance
builder owns the image format and authenticated release; the
[boot fixture](module-boot-fixture.md) owns isolated central, PostgreSQL,
HTTPS, DNS and NTP services. The generic-initramfs builder preserves production
bootstrap and configuration bytes while substituting the compatible ARM64
kernel/modules and adding a serial boot-report and test-control hook. Its manifest records each
substitution and verifies the preserved content after reopening.

The input `ci-image.json` contains the source commit, disk path/size/SHA-256,
generic boot directory, signed bundle directory and job-local deployment
directory. Before starting services, the harness verifies the disk checksum,
signed release, configuration digest, final artifact metadata and generic
kernel/initramfs checksums. It also binds the generic initramfs input to the
production initramfs recorded in the finalized image's PXE inventory.
The full generic module manifest has a shared 16 MiB producer/consumer limit;
small fixture JSON retains its separate 1 MiB limit.
The private rollback candidate has its own signed release and rootfs checksum.
Preflight binds both A/B identities to the same source, boot ABI and public
configuration, and verifies the recorded candidate fault. The candidate builder
reuses the prepared root, adds a Player service override that exits unsuccessfully,
and compresses once more before the configured root is discarded. It does not
repeat Ubuntu extraction, package installation or Player packaging. Candidate
bytes remain under the private deployment directory, outside image uploads.

Each run creates a fresh private fixture and a QEMU `virt` guest with two virtual
CPUs and 3 GiB RAM in a 4 GiB container. A software-rendered `virtio-gpu-pci`
device exposes up to two virtual DRM Outputs without host GPU access. The
generic initramfs validates and preloads its GPU module dependency closure
before switching to the signed Pi root; the root contains the Pi module tree.
The stock Player discovers real `Virtual-1`/`Virtual-2` connectors and uses the
same native GTK/GStreamer path and Weston kiosk routing as its HDMI Outputs. The disk and generic boot files are
mounted read-only; a private qcow2 overlay receives writes. There are no host
devices, host ports, guest credentials or replacement Player process. The
ordinary systemd Player from the signed root must register itself. Its network
service starts after Weston but does not inherit compositor restart jobs; a
missing DRM device must not create an enrollment restart loop. Each service
retains its own failure restart policy.

The automated scenarios require:

1. A successful signed HTTPS/DNS/NTP fixture probe and initially empty inventory.
2. A protected bootstrap report for the expected release and durable state,
   followed by one durable Player registration.
3. The first boot must select trial slot A. The production acceptance command
   must emit its successful, boot-bound completion event after the unchanged
   30-second Player health gate and durable promotion. No health report,
   acceptance state or replacement Player is injected by the fixture.
4. A VM power cycle using the same overlay, a different boot ID, and the same
   Player identity with a higher authority epoch. The protected boot report
   must now select the same release in accepted, non-trial slot A.
5. A central-service outage and restart, followed by a successful authenticated
   Player state request and unchanged Player identity/authority.
6. Through a read-only 9p share, publish a command bound to the accepted second
   boot. The test-only controller verifies that boot and invokes the production
   staging CLI for signed candidate B. Locked readback must show A unchanged
   and B pending but unconsumed before the controller requests the initial reboot.
7. Observe the protected B trial boot. Its failed Player cannot satisfy the
   unchanged health gate. The production recovery predicate must authenticate
   the failed trial and fallback, emit its boot-bound completion event, and the
   production recovery service must reboot into accepted A. The harness does
   not request that fallback reboot or select/reject/promote any slot.
8. Observe the fourth distinct boot ID, original accepted release A and same
   Player identity with a higher authority epoch. Cleanup only recorded test
   resources and verify the original disk hash remains unchanged.

Boot enrollment is bounded to fifteen minutes per emulated boot. The earlier
ten-minute deadline cut off a run after verification reached health but before
its health window could finish; [the failed run](evidence/2026-09-06-vm-rollback.md#corrected-verification-reached-health-enrollment-deadline-failed)
does not prove that a longer wait will produce enrollment. Observing trial acceptance
after enrollment is bounded to 540 seconds. Slot verification has a separate
300-second deadline before the unchanged 180-second health deadline and
30-second continuous-health requirement. The update lock spans verification,
fresh health observation and promotion. The production unit has a 510-second
cap; a verification delay cannot age the final health interval.
Reconnection is bounded to two minutes. Candidate staging has a 900-second
guest limit and 930-second host observation limit; production recovery evidence
has 870 seconds, covering the 510-second acceptance unit, 320-second recovery
unit and observation margin. The recovery predicate separately bounds fallback
verification to 300 seconds. The base VM process budget is 5,100 seconds,
covering three 900-second boot waits, reconnection, staging, recovery and
480 seconds of command margin. Real-media mode adds two 420-second presentation
waits and 60 seconds of margin, for 6,000 seconds. Offline cache probes run only
while the VM is stopped and have separate 300-second limits.
Container logs are capped. The public
JSON report contains artifact identities, observed boot/enrollment results,
sanitized failure codes and explicit qualification limits. Private TLS keys,
Player identity state and raw guest disks are excluded from test-report uploads.
Input validation failures also emit an unqualified report marked `preflight`,
without copying unverified artifact fields or starting fixture services.
Validated boot reports are retained across polls so serial-log rollover cannot
erase earlier boot evidence while enrollment is pending. Diagnostic output
also retains boot-bound `verifying` and `health` phase events with host observation
timestamps. These diagnostics locate a delayed phase and cannot qualify acceptance.
Diagnostic output
includes only bounded numeric/named exits for fixed system services and the
final inventory count, rather than raw service messages.
Mount-namespace failures additionally map a fixed set of system paths and
error messages to public labels and errno names. Unknown paths/messages become
`unclassified`; raw guest paths, messages and credentials remain excluded.
The parser follows the [systemd v255 mount-failure format](https://github.com/systemd/systemd/blob/v255/src/core/exec-invoke.c)
and bounds each service's distinct records to eight. These diagnostics locate
a startup failure; they cannot turn a failed enrollment into qualification.
The volatile CI health observer starts independently after the Player service.
It reads the private health report, fixed Player/Weston/acceptance service states,
and Wayland socket status. At most 60 boot-bound samples over 600 seconds expose
only allowlisted states, health freshness, booleans and the optional fixed
`health_reason` defined by the [Player service](module-player-service.md).
Legacy reports without that reason remain readable; newer reports require
the reason to agree with the health boolean. Unknown or contradictory values
fail closed. No Player identifier,
credential or arbitrary command/file content is exported. The host checks that
schema, retains samples only for observed boots, and records the observer source
hash. These samples are diagnostic and cannot establish acceptance or rendering.
A known failure of the first A acceptance service terminates its host wait
immediately; a passing gate still requires the production completion event.
The observer has a read-only service filesystem and no update/health write path.

Cleanup checks container identities before stopping or removing them; replacement
resources fail closed. VM cleanup, fixture cleanup and the original disk check
are attempted independently; a failure in one cannot suppress the others, and
any cleanup failure prevents a passing qualification result.

The VM has no physical panels. A pass of the strengthened gate qualifies generic
userspace boot, durable enrollment/reconnection, healthy-trial acceptance
using native initialization on virtual DRM, and automatic rollback of the signed
failed candidate. Without the media extension below, `native_rendering` remains
false. Neither mode establishes Pi firmware, EEPROM/PXE networking, onboard
Ethernet or dual HDMI; physical qualification fields remain false.
A failure or cleanup error clears all qualification fields. Physical scenarios
require a Pi bench with remote power, serial/network access and display capture.

## Real-media extension

GitHub Actions supplies `--worker-image` to both build and boot commands. Its
immutable production ARM64 worker image ID joins the central and builder IDs
in `ci-image.json`; the media gate rejects runtime IDs that differ from that
manifest. The worker has its own BuildKit cache and shares central dependency
layers. The upstream fixture reuses the loaded central image, avoiding a second
application build. Omitting the worker selects the historical boot-only mode,
which cannot set `native_rendering` true.

`scripts/appliance_media.py` owns a fresh disposable Immich 2.5.6 fixture through
the existing `FixtureHost`. It uploads the existing synthetic fixture, takes
the portrait's recorded capture instant and original SHA-256, and privately
wraps its read-only connection credential with the production worker loader.
The optional `BootFixture` media mode shares a media volume read-only with
central and read-write with the worker. Only the worker joins the borrowed
upstream network; its identity and Compose labels are checked, and boot-fixture
cleanup never deletes it. Owned resource cleanup precedes upstream cleanup.

After A1's native healthy acceptance, the operator helper creates a Frame for
one actual connected Output, binds and commits calibration, configures a
one-second image-only favorite query around that portrait, and schedules a
looping photo Scene. It uses authenticated operator HTTP and does not write
readiness, media, commits or observations into the database. The first Program
starts 90 seconds ahead and runs for two hours, covering the bounded reboot
scenario.

The read-only evidence query joins a ready production worker result and ready
media blob to the secured assignment, offered Plan, current configuration,
latest readiness, valid commit, committed coordination group and presented
observation. Player, epoch, Plan/revision, assignment, Frame/Output, binding,
original source hash and converted JPEG hash must agree. Presentation must be
inside the assignment and Plan intervals. The latest readiness sequence may
exceed the commit's historical sequence; both are explicitly reported, and
the production coordinator owns the historical readiness decision. This is
not inferred from the latest renewed commit timestamp: the bounded host wait
retains up to 64 earlier real SQL grant snapshots. A prior grant may support a
later drawing only for the same Player/epoch/Plan/revision/assignment/group,
with a timestamp before that drawing and a still-valid current commit. These
public snapshots return only to the test helper, never a production write API.
These records qualify native behavior only when paired with the exact VM's stock Player/renderer.
Unit tests using synthetic observations do not qualify rendering.

A1 must present the selected photo before the existing power cycle. With QEMU
stopped, a separate networkless libguestfs container opens the qcow2 overlay
read-only, locates the unique `PWSTATE` ext4 filesystem, verifies the production
state marker and rejects symlinks along `/player/cache/<sha>.blob`. It verifies
the exact bounded size and SHA-256 without exporting media bytes. Its temporary
filesystem/tooling permissions are confined to that container; both original
disk and VM directory mounts are read-only. After this first stopped-disk proof, a separate owned `media-control` volume
holds a fixed denial marker. Central mounts it read-only and the checked worker
acts as the test writer; production media cleanup owns neither the mount nor its
contents. The CI gateway denies media bodies with a fixed 503 response while
control/enrollment stays available. Denial persists across central restart and
is verified for the exact photo URL before and after each later drawing.
A2 and restored A3 must each
present a newly committed assignment using the same converted bytes under this
delivery denial, and the
stopped A3 disk must retain the same cache object. No additional boot is inserted.

The harness also checks exact container network memberships and verifies DNS
and numeric TCP denial to Immich from the VM's outer egress namespace before
and after rollback. This test-driver probe does not put upstream configuration
inside the Player. Passing media mode enables `native_rendering` and the
populated-cache check; physical qualification remains false. Actual image
qualification is still pending; [local preparation evidence](evidence/2026-09-06-vm-media.md)
separately records real services, tiny filesystems and synthetic record tests.

The first hosted virtual-GPU/native-trial run failed at its former acceptance
service deadline after durable enrollment; [the failure record](evidence/2026-09-05-github-image.md#native-trial-image-failure-at-afff7b1)
does not establish which phase caused that timeout. The corrected acceptance
ordering and subsequent rollback extension are not yet qualified by a hosted
boot. Their focused local checks and actual Linux module/option probes are
prerequisites, not substitute image evidence. Earlier hosted passes below used
the enrollment-only gate and retain their explicit false native/trial fields.

The first [hosted passing artifact](evidence/2026-09-05-github-image.md#first-hosted-exact-image-boot-pass)
completed all five scenarios at feature head `1eb16ef`, actual PR merge source
`61b9950dde9e2d61e149ed7927abe974d306a88a`. The public report retains exact
hashes and explicit false hardware/native qualification fields. Later
revisions require their own workflow result.
