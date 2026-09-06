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
kernel/modules and adding a serial boot-report hook. Its manifest records each
substitution and verifies the preserved content after reopening.

The input `ci-image.json` contains the source commit, disk path/size/SHA-256,
generic boot directory, signed bundle directory and job-local deployment
directory. Before starting services, the harness verifies the disk checksum,
signed release, configuration digest, final artifact metadata and generic
kernel/initramfs checksums. It also binds the generic initramfs input to the
production initramfs recorded in the finalized image's PXE inventory.

Each run creates a fresh private fixture and a QEMU `virt` guest with two virtual
CPUs and 3 GiB RAM in a 4 GiB container. The disk and generic boot files are
mounted read-only; a private qcow2 overlay receives writes. There are no host
devices, host ports, guest credentials or replacement Player process. The
ordinary systemd Player from the signed root must register itself.

The automated scenarios require:

1. A successful signed HTTPS/DNS/NTP fixture probe and initially empty inventory.
2. A protected bootstrap report for the expected release and durable state,
   followed by one durable Player registration.
3. A VM power cycle using the same overlay, a different boot ID, and the same
   Player identity with a higher authority epoch.
4. A central-service outage and restart, followed by a successful authenticated
   Player state request and unchanged Player identity/authority.
5. Cleanup of only recorded test resources and an unchanged original disk hash.

Boot enrollment is bounded to ten minutes per boot, reconnection to two minutes,
and each VM process to fifteen minutes. Container logs are capped. The public
JSON report contains artifact identities, observed boot/enrollment results,
sanitized failure codes and explicit qualification limits. Private TLS keys,
Player identity state and raw guest disks are excluded from test-report uploads.
Cleanup checks container identities before stopping or removing them; replacement
resources fail closed.

The headless VM has no physical panels. Its pass qualifies generic userspace
boot, durable enrollment and reconnection only. It does not qualify native
rendering, healthy-trial acceptance, automatic update rollback, Pi firmware,
EEPROM/PXE networking, onboard Ethernet or dual HDMI. Those fields remain false
in the report. Updater fault tests are separate evidence, and physical scenarios
require a Pi bench with remote power, serial/network access and display capture.
