#!/bin/sh
# Assemble the photo-wall netboot base bundle (0008 p4-boot-chain s2b).
#
# Given a built rpi-image-gen base squashfs plus the kernel/initrd/DTBs from a
# scratch Debian-trixie-arm64 root (linux-image-rpi-2712 + raspi-firmware +
# initramfs-tools), this stages the FROZEN bundle layout, writes config.txt and
# a cmdline.txt template, computes a corruption-only SHA256SUMS over every
# artifact, and content-verifies the initrd via scripts/verify_netboot_initrd.py.
#
#   photo-wall-base-bundle/
#     boot/
#       config.txt              (firmware directives; kernel + initramfs + dtb)
#       cmdline.txt             (TEMPLATE -- operator injects base_url/base_sha256)
#       kernel_2712.img         (rpi-2712 kernel the Pi 5 SPI-EEPROM bootloader fetches)
#       initrd.img              (mkinitramfs output carrying the slim netboot init)
#       bcm2712-rpi-5-b.dtb     (Pi 5 device tree)
#       overlays/*.dtbo         (optional device-tree overlays)
#     photo-wall-base.squashfs  (RAM-root, fetched over HTTP at boot)
#     SHA256SUMS                (sha256 of every artifact above, incl. squashfs)
#
# This is deliberately a portable shell script, NOT part of appliance/build.py:
# build.py and the old signed pipeline are slated for p4-retire, and this
# assembly must survive that deletion (owner: portable packages). It shells out
# only to coreutils + the repo's own stdlib verify script; no image-build tool
# glue leaks into it.
set -eu

usage() {
    cat >&2 <<'EOF'
usage: build_netboot_bundle.sh --kernel FILE --initrd FILE --dtb FILE \
           --squashfs FILE --output DIR --verify SCRIPT [options]

required:
  --kernel FILE       rpi-2712 kernel image (staged as boot/kernel_2712.img)
  --initrd FILE       mkinitramfs output    (staged as boot/initrd.img)
  --dtb FILE          Pi 5 device tree       (staged as boot/bcm2712-rpi-5-b.dtb)
  --squashfs FILE     rpi-image-gen base squashfs (staged as photo-wall-base.squashfs)
  --output DIR        bundle output directory (created; must be empty/absent)
  --verify SCRIPT     path to verify_netboot_initrd.py (run against --initrd)

optional:
  --overlays-dir DIR  directory of *.dtbo overlays (staged under boot/overlays/)
  --python BIN        interpreter for the verify script (default: python3)
  --skip-verify       skip the lsinitramfs content-verify (local dev only;
                      lsinitramfs is unavailable off an initramfs-tools host)
EOF
    exit 2
}

KERNEL=""
INITRD=""
DTB=""
SQUASHFS=""
OUTPUT=""
VERIFY=""
OVERLAYS_DIR=""
PYTHON="python3"
SKIP_VERIFY=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --kernel) KERNEL="$2"; shift 2 ;;
        --initrd) INITRD="$2"; shift 2 ;;
        --dtb) DTB="$2"; shift 2 ;;
        --squashfs) SQUASHFS="$2"; shift 2 ;;
        --output) OUTPUT="$2"; shift 2 ;;
        --verify) VERIFY="$2"; shift 2 ;;
        --overlays-dir) OVERLAYS_DIR="$2"; shift 2 ;;
        --python) PYTHON="$2"; shift 2 ;;
        --skip-verify) SKIP_VERIFY=1; shift ;;
        -h|--help) usage ;;
        *) echo "build_netboot_bundle: unknown argument: $1" >&2; usage ;;
    esac
done

for pair in "kernel:$KERNEL" "initrd:$INITRD" "dtb:$DTB" \
            "squashfs:$SQUASHFS" "output:$OUTPUT" "verify:$VERIFY"; do
    name=${pair%%:*}
    value=${pair#*:}
    if [ -z "$value" ]; then
        echo "build_netboot_bundle: --$name is required" >&2
        usage
    fi
done

for pair in "kernel:$KERNEL" "initrd:$INITRD" "dtb:$DTB" "squashfs:$SQUASHFS"; do
    name=${pair%%:*}
    value=${pair#*:}
    if [ ! -f "$value" ]; then
        echo "build_netboot_bundle: --$name file not found: $value" >&2
        exit 1
    fi
done
if [ ! -f "$VERIFY" ]; then
    echo "build_netboot_bundle: --verify script not found: $VERIFY" >&2
    exit 1
fi

if [ -e "$OUTPUT" ] && [ -n "$(ls -A "$OUTPUT" 2>/dev/null)" ]; then
    echo "build_netboot_bundle: --output must be empty or absent: $OUTPUT" >&2
    exit 1
fi

# Pick a sha256 tool: coreutils sha256sum on the CI runner, shasum on macOS.
if command -v sha256sum >/dev/null 2>&1; then
    sha256_cmd="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
    sha256_cmd="shasum -a 256"
else
    echo "build_netboot_bundle: no sha256sum or shasum on PATH" >&2
    exit 1
fi

boot_dir="$OUTPUT/boot"
mkdir -p "$boot_dir/overlays"

# Stage the firmware-fetched artifacts under boot/ with the FROZEN names the Pi
# 5 SPI-EEPROM bootloader expects (0008 boot-chain bundle-layout decision).
cp -- "$KERNEL" "$boot_dir/kernel_2712.img"
cp -- "$INITRD" "$boot_dir/initrd.img"
cp -- "$DTB" "$boot_dir/bcm2712-rpi-5-b.dtb"

overlay_count=0
if [ -n "$OVERLAYS_DIR" ] && [ -d "$OVERLAYS_DIR" ]; then
    for dtbo in "$OVERLAYS_DIR"/*.dtbo; do
        [ -f "$dtbo" ] || continue
        cp -- "$dtbo" "$boot_dir/overlays/"
        overlay_count=$((overlay_count + 1))
    done
fi
echo "build_netboot_bundle: staged $overlay_count device-tree overlay(s)"

# The (large) RAM-root squashfs is fetched over HTTP at boot, so it sits at the
# bundle root, NOT under the TFTP-served boot/ directory (transport split).
cp -- "$SQUASHFS" "$OUTPUT/photo-wall-base.squashfs"

# config.txt: Pi 5 uses the SPI-EEPROM bootloader and needs NO start*.elf /
# fixup*.dat (those are Pi 4 and earlier). config.txt is MANDATORY on Pi 5 --
# its presence marks a TFTP prefix as bootable. Comments (#) are honored here
# (unlike cmdline.txt, which is a single literal kernel command line).
cat > "$boot_dir/config.txt" <<'EOF'
# Raspberry Pi 5 (BCM2712) netboot base bundle -- photo-wall p4-boot-chain.
# The Pi 5 boots from its SPI EEPROM; no start*.elf/fixup*.dat are required or
# shipped. This file is mandatory: the bootloader treats a TFTP prefix as
# bootable only when config.txt is present.
arm_64bit=1
# The rpi-2712 kernel (16K pages, Pi 5 optimised). The firmware defaults to
# kernel_2712.img on a Pi 5; naming it explicitly keeps the bundle unambiguous.
kernel=kernel_2712.img
# Load this bundle's initramfs immediately after the kernel image.
initramfs initrd.img followkernel
# The only device tree this bundle ships (Pi 5 model B). The firmware would
# auto-select a matching-named DTB; the explicit directive is belt-and-braces
# for a single-board fleet.
device_tree=bcm2712-rpi-5-b.dtb
disable_overscan=1
EOF

# cmdline.txt is a TEMPLATE, not a bootable command line: the operator boot
# server MUST substitute the two @@...@@ placeholders before serving it over
# TFTP (base_url/base_sha256 are injected by the operator, never baked -- the
# transport-split decision). NOTE: the Pi firmware passes cmdline.txt verbatim
# to the kernel and does NOT support comments, so the leading comment line MUST
# be deleted; cmdline.txt must end up a single line. The @@ placeholders also
# guarantee this file cannot be booted unedited.
cat > "$boot_dir/cmdline.txt" <<'EOF'
# TEMPLATE -- delete this comment line, keep ONE command line. Substitute:
#   @@PHOTOWALL_BASE_URL@@    -> http(s) URL of photo-wall-base.squashfs (required)
#   @@PHOTOWALL_BASE_SHA256@@ -> 64-hex corruption checksum of it (optional; else drop the token)
console=tty1 ip=dhcp boot=photowall-netboot panic=10 photowall.base_url=@@PHOTOWALL_BASE_URL@@ photowall.base_sha256=@@PHOTOWALL_BASE_SHA256@@
EOF

# Corruption-only SHA256SUMS over every staged artifact (incl. the squashfs).
# Paths are relative to the bundle root so the file is position-independent.
(
    cd "$OUTPUT"
    find . -type f ! -name SHA256SUMS -print0 \
        | LC_ALL=C sort -z \
        | xargs -0 $sha256_cmd > SHA256SUMS
)
echo "build_netboot_bundle: wrote SHA256SUMS ($(wc -l < "$OUTPUT/SHA256SUMS") entries)"

# Content-verify the initrd against the s2a slim-init contract (no hardware).
if [ "$SKIP_VERIFY" -eq 1 ]; then
    echo "build_netboot_bundle: --skip-verify set; NOT running verify_netboot_initrd"
else
    echo "build_netboot_bundle: verifying $boot_dir/initrd.img"
    "$PYTHON" "$VERIFY" "$boot_dir/initrd.img"
fi

echo "build_netboot_bundle: bundle assembled at $OUTPUT"
