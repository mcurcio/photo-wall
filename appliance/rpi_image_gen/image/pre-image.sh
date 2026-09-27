#!/bin/sh
# rpi-image-gen IMAGE_ASSET pre-image hook -- invoked by bin/runner as
# `pre-image.sh <IGconf_target_path> <IGconf_image_outputdir>` (see bin/ig's
# `pre-image)` argument-assembly case). Templates rootfs.cfg.in with this
# build's image.name/image.suffix and drops it into the image outputdir as
# the sole genimage config fragment. No device layer and no SRCROOT-level
# pre-image.sh exist in this build, so genimage01.cfg here is the entire
# genimage.cfg bin/ig's generate_images() globs for
# (mirrors examples/nested_image/image/embedded_squashfs/pre-image.sh's
# templating pattern, minus its hdimage/vfat/ext4 partitioning -- we emit a
# single flat squashfs image block instead).
#
# It also removes the build host's etc/resolv.conf from the rootfs (this hook
# runs after mmdebstrap's own cleanup): stage 1 writes stage 2's resolver
# into the new root at boot (appliance/netboot_init.py hand_over_resolver),
# and base-image.yml refuses an image that carries one.
set -eu

target=$1
outputdir=$2

rm -f "${target}/etc/resolv.conf"

sed \
   -e "s|<IMAGE_NAME>|$IGconf_image_name|g" \
   -e "s|<IMAGE_SUFFIX>|$IGconf_image_suffix|g" \
   rootfs.cfg.in > "${outputdir}/genimage01.cfg"
