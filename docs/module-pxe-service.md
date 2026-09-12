# Common PXE service

Status: bounded service contract and tftpd-hpa mapping implemented, 2026-09-05. This module supplies one public boot tree to every supported Player. It does not qualify a physical Raspberry Pi boot, a broadcast LAN, DHCP ownership, or EEPROM configuration.

**This is the D1 netboot enhancement, not the baseline.** The [0008 baseline](decisions/0008-generic-image-and-serial-identity.md) is **D0**: flash the generic image to SD/USB and boot — no boot server, no TFTP, no DHCP changes required (see the [README](../README.md#provision-player-appliances) and the [flash-and-go runbook path](runbook.md#player-provisioning-flash-and-go)). Netboot trades that flash step for the boot-server infrastructure documented here, in exchange for stateless RAM-root delivery. Identity (hardware serial) and enrollment work the same way on both delivery tiers once the Player process starts; this module owns only how the OS itself reaches the Pi.

**The transport this module describes (TFTP, a boot-server tree) stays the same across both netboot models; the content it serves is mid-migration.** As built today, the tree carries the signed, combined base+app image from [decision 0008](decisions/0008-generic-image-and-serial-identity.md#the-netboot-tier-d1-and-its-config-decoupling). [Decision 0009](decisions/0009-minimal-base-and-app-package.md) is the adopted target: the tree instead carries an **unsigned minimal base image** with no application baked in (gate #4 — "base squashfs staged in the boot-server tree, loaded by initramfs; no central, no signature"), and a small in-image bootstrapper fetches the Player as a `.deb` from central at boot. That base image already builds (`scripts/build_ci_base_image.py`) and the bootstrapper already exists (`appliance/provision.py`), but the PXE boot chain that would load this minimal base and hand off to the bootstrapper is **not yet wired** — see [the appliance builder module](module-appliance-builder.md#the-0009-minimal-base-and-bootstrapper-in-progress). Everything below (the map file, tftpd-hpa/dnsmasq configuration, the prefix-stripping rules) applies unchanged to either tree's content; only what gets staged beneath the root changes.

## Design

### Responsibility

The PXE service publishes the finalized, common Raspberry Pi boot tree over TFTP. It adapts the bootloader's optional device-specific filename prefix into that common tree at the TFTP boundary. The service owns no Player identity, enrollment decision, per-device image, or private central data. A serial/MAC value used by the bootloader is routing metadata only; enrollment remains the Player-to-central TLS contract.

### Inputs

The operator supplies:

- a finalized common PXE tree containing `config.txt`, the selected Pi 5 kernel/DTBs, the explicitly named initramfs, and the signed release/rootfs bundle;
- the TFTP server's operator-chosen wired interface, server IP address, and PXE subnet; and
- the existing DHCP/DNS arrangement, central HTTPS origin, and configured provisioning time sources.

The tree is staged beneath a dedicated public TFTP root. The public root contains only files intended for unauthenticated boot download, with no private keys, enrollment material, credentials, central database data, or private media. The service expects the supported Pi EEPROM network mode and boot order to already be configured; EEPROM programming and interactive imaging remain outside this module.

The Raspberry Pi documentation describes the default `TFTP_PREFIX=0` behavior as a serial-number directory such as `9ffefdef/`. The mapping therefore accepts one leading directory consisting of exactly eight hexadecimal characters. A request without that prefix is also valid because all clients receive the same common tree. [Raspberry Pi TFTP prefix configuration](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#tftp-prefix).

### Outputs

The checked-in [`tftpd.map`](../appliance/provisioning/tftpd.map) is the service's complete filename policy for tftpd-hpa. The operator's service configuration points tftpd-hpa at that map and at the finalized public root, using `--secure` so the root is its chroot. A deployment may use inetd/systemd packaging supplied by the operating system; this module does not start a daemon or take over the user's LAN.

### Invariants and failure behavior

- Every served path remains below the public TFTP root. Absolute names, `..` path components, and backslashes are denied before any prefix rewrite.
- Every WRQ/PUT is denied. The tree is read-only; files do not become writable merely because a client requests them.
- At most one leading 8-hex directory is removed for an RRQ/GET. The `g` repeat operation is intentionally absent, so `deadbeef/deadbeef/kernel_2712.img` is not silently reduced twice.
- A non-hex prefix, a malformed prefix, an unknown file, or a second prefix resolves to no public file and does not trigger registration, image generation, or a fallback to a private location.
- The map performs no serial/MAC lookup. There is no individual device image and no manual prefix registration step.
- `--secure` and a dedicated low-privilege service account restrict the daemon to the public root. The root and its boot files must be readable by that account and must not contain private files. tftpd-hpa's own public-read checks remain enabled.
- TFTP is only the trusted-LAN bootstrap transport. The fetched signed/bounded release, HTTPS origin, DNS, and chrony/provisioning time sources must already resolve and validate after bootstrap. TFTP signing cannot authenticate a hostile LAN or EEPROM bootstrap.

If the finalized tree, map, or operator network values are absent or invalid, configuration must fail before the service is enabled. If a request violates a rule, tftpd-hpa returns access denied; if the resulting public name is absent, it returns file not found. Neither condition mutates state or grants execution authority.

## Central operator configuration examples

These are configuration fragments with required operator values left explicit. They are not commands to run on a user LAN. The existing DHCP server remains authoritative in both examples.

### tftpd-hpa with existing DHCP

Choose values for `<PXE_INTERFACE>`, `<PXE_SERVER_IP>`, `<PXE_SUBNET_CIDR>`, and `<PUBLIC_PXE_ROOT>` from the deployment network. Bind the service to the selected server address and keep the root dedicated to public boot files:

```ini
# /etc/default/tftpd-hpa (operator-provided values)
TFTP_USERNAME="tftp"
TFTP_DIRECTORY="<PUBLIC_PXE_ROOT>"
TFTP_ADDRESS="<PXE_SERVER_IP>:69"
TFTP_OPTIONS="--secure --ipv4 --map-file /etc/photo-wall/tftpd.map"

# The host firewall and service binding must permit UDP/69 only on
# <PXE_INTERFACE> / <PXE_SUBNET_CIDR>, according to the operator's policy.
```

The existing DHCP server must advertise the chosen `<PXE_SERVER_IP>` as the next server and the common boot filename required by the selected Pi EEPROM configuration. DHCP reservations or leases remain the operator's responsibility. No service fragment here enables DHCP or changes DNS.

### dnsmasq proxy-PXE with existing DHCP

Where the existing DHCP server cannot provide PXE options, dnsmasq can provide proxy-PXE replies while leaving address assignment with that server. Choose `<PXE_INTERFACE>`, `<PXE_SERVER_IP>`, `<PXE_SUBNET_ADDRESS>`, and `<PXE_NETMASK>`; retain the existing DHCP service and restrict dnsmasq to the operator-approved interface:

```ini
# /etc/dnsmasq.d/photo-wall-proxy-pxe.conf (operator-provided values)
interface=<PXE_INTERFACE>
bind-interfaces
port=0
dhcp-range=<PXE_SUBNET_ADDRESS>,proxy,<PXE_NETMASK>
pxe-service=0,"Raspberry Pi Boot",bootcode.bin,<PXE_SERVER_IP>
```

The `<PXE_SUBNET_ADDRESS>` value is the IPv4 network address, with `<PXE_NETMASK>` supplied as the separate mask; this is not CIDR notation. The exact boot filename and Pi-specific PXE option required by the chosen EEPROM/firmware revision must be confirmed against the finalized tree and the operator's DHCP design. `port=0` disables dnsmasq's DNS listener for this proxy-PXE-only fragment. dnsmasq's built-in TFTP server is not used; tftpd-hpa remains the sole TFTP server at `<PXE_SERVER_IP>`. The proxy configuration must not be enabled on an unapproved interface or subnet.

### Public dependency preconditions

Before a Player can boot successfully, the operator must make the central HTTPS origin, its DNS name, certificate chain, and configured provisioning time sources resolvable from the trusted provisioning network. A valid clock is established before HTTPS release validation. These are deployment inputs and qualification gates, not generated by the TFTP service. The trusted wired LAN, server IP/subnet, and Pi EEPROM network mode are explicit bootstrapping preconditions.

## Acceptance checks

The integration test uses the actual `in.tftpd` binary when all gates are met: Linux, root, a discovered remap-capable daemon, a high ephemeral UDP port, and a temporary chroot containing synthetic public files. The test accepts the packaged daemon's `:port` bind form only inside a namespace whose enumerated IPv4 addresses are all loopback; otherwise it skips before starting the daemon. A bounded fixed RRQ client checks exact bytes for prefixed and unprefixed names, and checks access denial for traversal, absolute names, backslashes, and WRQ. It never opens a user-LAN socket.

On non-Linux hosts, without `in.tftpd`, without remap support, without root, or outside a loopback-only IPv4 namespace, the real-daemon test is explicitly skipped. A skipped or passing local test does not qualify a broadcast LAN. Actual Pi PXE, DHCP/proxy-PXE, DNS, HTTPS, chrony, EEPROM, and physical display behavior remain unqualified until an isolated Linux PXE host and Pi bench are available.

The service design follows the tftpd-hpa Bookworm manual: `--secure` chroots the daemon, `--map-file` enables remapping, `a` refuses requests, `G` limits a rule to GET, `P` limits it to PUT, and rules use egrep-style regular expressions processed from top to bottom. The proxy fragment follows dnsmasq's documented IPv4 `proxy` range and `pxe-service` forms; the installed Noble `dnsmasq-base` parser is an acceptance check for the concrete fixture. [Debian tftpd-hpa manual](https://manpages.debian.org/bookworm/tftpd-hpa/in.tftpd.8.en.html), [dnsmasq documentation](https://dnsmasq.org/doc.html), [Debian dnsmasq-base manual](https://manpages.debian.org/bookworm/dnsmasq-base/dnsmasq.8.en.html)

The isolated Linux arm64 gate ran on 2026-09-05 from `photo-wall-appliance-builder:20260905`, with `tftpd-hpa 5.2+20150808-1.4build1` (remap enabled) and `dnsmasq-base 2.91-0ubuntu0.24.04.1`. It ran one pytest case successfully under `--network none`, with 0.5 CPU and 256 MiB memory, using only loopback packets and a temporary chroot; no DHCP daemon or user-LAN socket was started. This qualifies the tested daemon/map and concrete dnsmasq parser only.
