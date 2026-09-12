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
set -eu

outputdir=$2

sed \
   -e "s|<IMAGE_NAME>|$IGconf_image_name|g" \
   -e "s|<IMAGE_SUFFIX>|$IGconf_image_suffix|g" \
   rootfs.cfg.in > "${outputdir}/genimage01.cfg"
