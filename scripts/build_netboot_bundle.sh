#!/bin/sh
# Assemble the photo-wall netboot base bundle (0008 p4-boot-chain s2b).
#
# Given a built rpi-image-gen base squashfs plus the kernel/initrd/DTBs from a
# scratch Debian-trixie-arm64 root (linux-image-rpi-2712 + raspi-firmware +
# initramfs-tools), this stages the FROZEN bundle layout, puts the boot data in
# front of the cached initrd (scripts/build_boot_data.py: stage 1's computed
# closure, the base's CA bundle and the clock floor; decision 0014 §5), writes
# config.txt and a cmdline.txt template, computes a corruption-only SHA256SUMS
# over every artifact, and content-verifies the initrd via
# scripts/verify_netboot_initrd.py against the closure manifest.
#
#   photo-wall-base-bundle/
#     boot/
#       config.txt              (firmware directives; kernel + initramfs + dtb)
#       cmdline.txt             (TEMPLATE -- operator sets Central's root ONCE)
#       kernel_2712.img         (rpi-2712 kernel the Pi 5 SPI-EEPROM bootloader fetches)
#       initrd.img              (boot data, then the cached mkinitramfs output)
#       bcm2712-rpi-5-b.dtb     (Pi 5 device tree)
#       overlays/*.dtbo         (optional device-tree overlays)
#       pieeprom.upd            (bootloader self-update: BOOT_ORDER=0xf21, BOOT_WATCHDOG_TIMEOUT=120)
#       pieeprom.sig            (rpi-eeprom-digest over pieeprom.upd)
#     photo-wall-base.squashfs  (RAM-root, fetched over HTTP at boot)
#     SHA256SUMS                (sha256 of every artifact above, incl. squashfs)
#
# This is deliberately a portable shell script, NOT part of appliance/build.py:
# build.py and the old signed pipeline are slated for p4-retire, and this
# assembly must survive that deletion (owner: portable packages). It shells out
# only to coreutils, git, unsquashfs and the repo's own stdlib build scripts;
# no image-build tool glue leaks into it.
set -eu

usage() {
    cat >&2 <<'EOF'
usage: build_netboot_bundle.sh --kernel FILE --initrd FILE --dtb FILE \
           --squashfs FILE --eeprom-image FILE --output DIR --verify SCRIPT \
           --repo DIR --python-libdir DIR [options]

required:
  --kernel FILE       rpi-2712 kernel image (staged as boot/kernel_2712.img)
  --initrd FILE       cached mkinitramfs output (boot/initrd.img is the boot
                      data followed by these bytes unchanged)
  --dtb FILE          Pi 5 device tree       (staged as boot/bcm2712-rpi-5-b.dtb)
  --squashfs FILE     rpi-image-gen base squashfs (staged as photo-wall-base.squashfs)
  --eeprom-image FILE packaged rpi-eeprom bootloader image (source for pieeprom.upd/.sig)
  --output DIR        bundle output directory (created; must be empty/absent)
  --verify SCRIPT     path to verify_netboot_initrd.py (run against boot/initrd.img)
  --repo DIR          the checked-out revision: stage 1's closure and the clock
                      floor (its HEAD commit time) come from here
  --python-libdir DIR the initrd interpreter's stdlib dir (e.g. /usr/lib/python3.13)

optional:
  --overlays-dir DIR  directory of *.dtbo overlays (staged under boot/overlays/)
  --python BIN        interpreter for the verify script and eeprom_update.py (default: python3)
  --eeprom-update SCRIPT  path to scripts/eeprom_update.py (default: alongside this script)
  --rpi-eeprom-config FILE  the packaged rpi-eeprom-config to run (default: PATH lookup --
                      local dev with the real rpi-eeprom package installed; CI passes the
                      pinned scratch-root's own copy, never the runner's, which has none)
  --rpi-eeprom-digest FILE  the packaged rpi-eeprom-digest to run (default: PATH lookup)
  --build-boot-data SCRIPT  path to scripts/build_boot_data.py (default: alongside this script)
  --snapshot-epoch N  the Debian snapshot pin; build_boot_data.py warns past 90 days
  --base-abi-file FILE  ABI extracted from this squashfs by the build; binds schema-2 releases
  --skip-verify       skip the lsinitramfs content-verify (local dev only;
                      lsinitramfs is unavailable off an initramfs-tools host)
EOF
    exit 2
}

KERNEL=""
INITRD=""
DTB=""
SQUASHFS=""
EEPROM_IMAGE=""
OUTPUT=""
VERIFY=""
OVERLAYS_DIR=""
PYTHON="python3"
EEPROM_UPDATE=""
RPI_EEPROM_CONFIG=""
RPI_EEPROM_DIGEST=""
REPO=""
PYTHON_LIBDIR=""
BUILD_BOOT_DATA=""
SNAPSHOT_EPOCH=""
BASE_ABI_FILE=""
SKIP_VERIFY=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --kernel) KERNEL="$2"; shift 2 ;;
        --initrd) INITRD="$2"; shift 2 ;;
        --dtb) DTB="$2"; shift 2 ;;
        --squashfs) SQUASHFS="$2"; shift 2 ;;
        --eeprom-image) EEPROM_IMAGE="$2"; shift 2 ;;
        --output) OUTPUT="$2"; shift 2 ;;
        --verify) VERIFY="$2"; shift 2 ;;
        --overlays-dir) OVERLAYS_DIR="$2"; shift 2 ;;
        --python) PYTHON="$2"; shift 2 ;;
        --eeprom-update) EEPROM_UPDATE="$2"; shift 2 ;;
        --rpi-eeprom-config) RPI_EEPROM_CONFIG="$2"; shift 2 ;;
        --rpi-eeprom-digest) RPI_EEPROM_DIGEST="$2"; shift 2 ;;
        --repo) REPO="$2"; shift 2 ;;
        --python-libdir) PYTHON_LIBDIR="$2"; shift 2 ;;
        --build-boot-data) BUILD_BOOT_DATA="$2"; shift 2 ;;
        --snapshot-epoch) SNAPSHOT_EPOCH="$2"; shift 2 ;;
        --base-abi-file) BASE_ABI_FILE="$2"; shift 2 ;;
        --skip-verify) SKIP_VERIFY=1; shift ;;
        -h|--help) usage ;;
        *) echo "build_netboot_bundle: unknown argument: $1" >&2; usage ;;
    esac
done

if [ -z "$EEPROM_UPDATE" ]; then
    EEPROM_UPDATE="$(dirname -- "$0")/eeprom_update.py"
fi
if [ -z "$BUILD_BOOT_DATA" ]; then
    BUILD_BOOT_DATA="$(dirname -- "$0")/build_boot_data.py"
fi

for pair in "kernel:$KERNEL" "initrd:$INITRD" "dtb:$DTB" "squashfs:$SQUASHFS" \
            "eeprom-image:$EEPROM_IMAGE" "output:$OUTPUT" "verify:$VERIFY" \
            "repo:$REPO" "python-libdir:$PYTHON_LIBDIR"; do
    name=${pair%%:*}
    value=${pair#*:}
    if [ -z "$value" ]; then
        echo "build_netboot_bundle: --$name is required" >&2
        usage
    fi
done

for pair in "kernel:$KERNEL" "initrd:$INITRD" "dtb:$DTB" "squashfs:$SQUASHFS" \
            "eeprom-image:$EEPROM_IMAGE"; do
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
if [ ! -f "$EEPROM_UPDATE" ]; then
    echo "build_netboot_bundle: --eeprom-update script not found: $EEPROM_UPDATE" >&2
    exit 1
fi
if [ -n "$RPI_EEPROM_CONFIG" ] && [ ! -f "$RPI_EEPROM_CONFIG" ]; then
    echo "build_netboot_bundle: --rpi-eeprom-config file not found: $RPI_EEPROM_CONFIG" >&2
    exit 1
fi
if [ -n "$RPI_EEPROM_DIGEST" ] && [ ! -f "$RPI_EEPROM_DIGEST" ]; then
    echo "build_netboot_bundle: --rpi-eeprom-digest file not found: $RPI_EEPROM_DIGEST" >&2
    exit 1
fi
if [ ! -f "$BUILD_BOOT_DATA" ]; then
    echo "build_netboot_bundle: --build-boot-data script not found: $BUILD_BOOT_DATA" >&2
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
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# Stage the firmware-fetched artifacts under boot/ with the FROZEN names the Pi
# 5 SPI-EEPROM bootloader expects (0008 boot-chain bundle-layout decision).
cp -- "$KERNEL" "$boot_dir/kernel_2712.img"
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

# boot/initrd.img: the boot data in front of the cached initrd (decision 0014
# §5). The CA bundle is the BUILT BASE's, byte for byte (R5: the same list);
# the floor is this revision's commit time, never SOURCE_DATE_EPOCH (a
# hand-bumped snapshot pin). build_boot_data.py computes and stages stage 1's
# closure, refuses a future floor or a bundle with no certificate, and writes
# the manifest the verifier below reads.
unsquashfs -cat "$SQUASHFS" etc/ssl/certs/ca-certificates.crt > "$work/ca-certificates.crt"
floor=$(git -C "$REPO" log -1 --format=%ct)
"$PYTHON" "$BUILD_BOOT_DATA" --repo "$REPO" --cached-initrd "$INITRD" \
    --python-libdir "$PYTHON_LIBDIR" --ca-bundle "$work/ca-certificates.crt" \
    --floor "$floor" --out "$boot_dir/initrd.img" --manifest "$work/closure-manifest.json" \
    ${SNAPSHOT_EPOCH:+--snapshot-epoch "$SNAPSHOT_EPOCH"}

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
# Full KMS: enables the display pipeline (HDMI, HVS, pixel valves, the vc4
# gpu node) and the v3d GPU in the device tree, all disabled in the bare
# bcm2712-rpi-5-b.dtb, so the kernel's vc4 and v3d drivers find devices and
# /dev/dri exists. Named as the Pi 5 variant directly: the generic
# vc4-kms-v3d reaches it only through overlays/overlay_map.dtb, which this
# bundle does not ship (it stages *.dtbo only), and applied to this DTB the
# generic overlay fails. It also disables the legacy firmware framebuffer
# (brcm,bcm2708-fb); vc4's own framebuffer takes over the console.
# scripts/verify_boot_display.py checks this line, the overlay file, and the
# nodes it turns on.
dtoverlay=vc4-kms-v3d-pi5
EOF

# pieeprom.upd/.sig: the bootloader self-update carrying BOOT_ORDER=0xf21 and
# BOOT_WATCHDOG_TIMEOUT=120 (0014 rev 5, design §2.8). eeprom_update.py refuses
# (nonzero exit, caught by `set -eu`) if either setting is missing from the
# built update once read back, so this step alone is the "check calls" gate --
# a bundle whose self-update silently missed a setting never finishes assembly.
# --rpi-eeprom-config/-digest, when given, point at the SAME pinned rpi-eeprom
# package's tools (e.g. CI's scratch root) instead of eeprom_update.py's PATH
# lookup default -- the runner itself never gets rpi-eeprom installed.
"$PYTHON" "$EEPROM_UPDATE" --image "$EEPROM_IMAGE" --out "$boot_dir" \
    ${RPI_EEPROM_CONFIG:+--rpi-eeprom-config "$RPI_EEPROM_CONFIG"} \
    ${RPI_EEPROM_DIGEST:+--rpi-eeprom-digest "$RPI_EEPROM_DIGEST"}
for f in pieeprom.upd pieeprom.sig; do
    if [ ! -f "$boot_dir/$f" ]; then
        echo "build_netboot_bundle: eeprom_update.py did not write $boot_dir/$f" >&2
        exit 1
    fi
done

# cmdline.txt is a TEMPLATE, not a bootable command line: the operator (or the
# deployment staging it) replaces the ONE @@PHOTOWALL_CENTRAL@@ placeholder with
# Central's ROOT URL, e.g. http://photo-wall/ or http://10.0.20.5/ (required),
# before serving it over TFTP, once per site, and changes nothing else. This
# line is then STATIC and fleet-wide immortal:
# everything after the root -- the netboot request path (/v1/netboot/base), the
# Pi's identity (its serial, self-supplied in the X-PhotoWall-Serial header),
# and the expected corruption digest (the HTTP Digest response header) -- is
# auto-discovered by the initrd, so the base image never changes as the served
# squashfs is revised. NOTE: the Pi firmware passes cmdline.txt verbatim to the
# kernel and does NOT support comments, so the file is written as exactly ONE
# line and must stay one line: the placeholder is the only thing to change
# (contracts/release.py CMDLINE_PLACEHOLDER; the release seal refuses any other
# shape). The @@ placeholder also guarantees this file cannot be booted unedited.
#
# Optional: append `photowall.debug=1` to raise console verbosity and lengthen
# the pre-reboot pause on failure (field debugging on an HDMI/serial console).
#
# watchdog.stop_on_reboot=0 and hung_task_panic=1 (0014 rev 5, design §2.8):
# the two kernel liveness parameters. Neither is a photowall.* parameter, and
# neither can be set any other way before the moment each matters --
# `missing_kernel_liveness()` (appliance/bootstrap.py) names any that a
# running kernel does not show, so a template regression here is loud, not
# silent.
#
# cgroup_enable=memory (contracts/release.py CMDLINE_MEMORY_CONTROLLER), on
# every Pi: the Pi 5 device tree's bootargs, which the firmware puts before
# this file, carry cgroup_disable=memory (raspberrypi/linux#6980); the later
# cgroup_enable=memory wins. Without it no MemoryMax= is enforced; the node
# base still mounts its store and reports memcg_present 0. This template is the
# token's one source: the release seal refuses a cmdline without it, or with
# it twice (scripts/package_release_artifacts.py _check_cmdline).
#
# photowall.node=v2 (contracts/release.py CMDLINE_NODE_TOKEN): every Pi takes
# the node path. Like cgroup_enable=memory, this template is the token's one
# source and the seal refuses a cmdline without it, or with it twice.
cat > "$boot_dir/cmdline.txt" <<'EOF'
console=tty1 ip=dhcp boot=photowall-netboot panic=10 watchdog.stop_on_reboot=0 hung_task_panic=1 cgroup_enable=memory photowall.node=v2 photowall.central=@@PHOTOWALL_CENTRAL@@
EOF

# Corruption-only SHA256SUMS over every staged artifact (incl. the squashfs).
# Paths are relative to the bundle root so the file is position-independent.
if [ -n "$BASE_ABI_FILE" ]; then
    [ -f "$BASE_ABI_FILE" ] || { echo "base ABI file missing" >&2; exit 1; }
    ( cd "$REPO" && "$PYTHON" -c '
import hashlib, pathlib, sys
from contracts.release import base_abi_sidecar
abi_file, squashfs, output = map(pathlib.Path, sys.argv[1:])
abi = abi_file.read_text(encoding="ascii").strip()
digest = hashlib.file_digest(squashfs.open("rb"), "sha256").hexdigest()
output.write_bytes(base_abi_sidecar(abi, digest))
' "$BASE_ABI_FILE" "$OUTPUT/photo-wall-base.squashfs" "$OUTPUT/base-abi.json" )
fi
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
    "$PYTHON" "$VERIFY" "$boot_dir/initrd.img" --manifest "$work/closure-manifest.json"
fi

echo "build_netboot_bundle: bundle assembled at $OUTPUT"
