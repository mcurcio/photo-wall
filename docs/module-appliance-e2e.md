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

Boot enrollment is bounded to ten minutes per boot. Observing trial acceptance
after enrollment is bounded to 210 seconds; the production unit retains its
180-second health deadline and 30-second continuous-health requirement.
Reconnection is bounded to two minutes. Candidate staging has a 900-second
guest limit and 930-second host observation limit; production recovery evidence
has 330 seconds, covering the unchanged health and verified-fallback deadlines.
Each VM process has a one-hour cap covering staging and its subsequent boots.
Container logs are capped. The public
JSON report contains artifact identities, observed boot/enrollment results,
sanitized failure codes and explicit qualification limits. Private TLS keys,
Player identity state and raw guest disks are excluded from test-report uploads.
Input validation failures also emit an unqualified report marked `preflight`,
without copying unverified artifact fields or starting fixture services.
Validated boot reports are retained across polls so serial-log rollover cannot
erase earlier boot evidence while enrollment is pending. Diagnostic output
includes only bounded numeric/named exits for fixed system services and the
final inventory count, rather than raw service messages.
Mount-namespace failures additionally map a fixed set of system paths and
error messages to public labels and errno names. Unknown paths/messages become
`unclassified`; raw guest paths, messages and credentials remain excluded.
The parser follows the [systemd v255 mount-failure format](https://github.com/systemd/systemd/blob/v255/src/core/exec-invoke.c)
and bounds each service's distinct records to eight. These diagnostics locate
a startup failure; they cannot turn a failed enrollment into qualification.
Cleanup checks container identities before stopping or removing them; replacement
resources fail closed. VM cleanup, fixture cleanup and the original disk check
are attempted independently; a failure in one cannot suppress the others, and
any cleanup failure prevents a passing qualification result.

The VM has no physical panels. A pass of the strengthened gate qualifies generic
userspace boot, durable enrollment/reconnection, healthy-trial acceptance
using native initialization on virtual DRM, and automatic rollback of the signed
failed candidate. It does not establish a centrally committed native media draw,
preservation of a populated media cache, Pi firmware, EEPROM/PXE networking,
onboard Ethernet or dual HDMI. `native_rendering` and physical qualification
fields therefore remain false.
A failure or cleanup error clears all qualification fields. Physical scenarios
require a Pi bench with remote power, serial/network access and display capture.

The virtual-GPU/native-trial extension and subsequent rollback extension are
not yet qualified by a hosted boot. Their focused local checks and actual Linux module/option probes are
prerequisites, not substitute image evidence. Earlier hosted passes below used
the enrollment-only gate and retain their explicit false native/trial fields.

The first [hosted passing artifact](evidence/2026-09-05-github-image.md#first-hosted-exact-image-boot-pass)
completed all five scenarios at feature head `1eb16ef`, actual PR merge source
`61b9950dde9e2d61e149ed7927abe974d306a88a`. The public report retains exact
hashes and explicit false hardware/native qualification fields. Later
revisions require their own workflow result.
