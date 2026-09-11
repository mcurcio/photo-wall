#!/bin/sh
# First-boot grow of the generic flash image's ext4 root (0008 D0): the
# image is written at a fixed size, so this expands the root partition and
# filesystem to fill whatever SD/USB card it was flashed onto. Runs once
# (ConditionPathExists in grow-rootfs.service marks completion) before the
# Player starts. CI/hardware step: untestable on a non-Linux/non-root host.
set -eu

root_source=$(findmnt -no SOURCE /)
root_device=$(basename "$root_source")
partition_number=$(cat "/sys/class/block/$root_device/partition")
disk="/dev/$(lsblk -no PKNAME "$root_source")"

growpart "$disk" "$partition_number" || true
resize2fs "$root_source"

touch /etc/photo-wall/.grown
