#!/bin/sh
# Seal one release root (decision 0019, data flow step 3): what mmdebstrap built, as the Node
# stages it (appliance/apps/environment.py's contract and semantics).
#
#   debian-packaging/seal-hook.sh ROOT ROLE DEB ABI OUTPUT
#
# ROOT is mmdebstrap's finished root directory, ROLE `app` or `manager-primary`, DEB the root
# package's .deb in the local repo, ABI a directory holding base.json and display.json (the
# repo's photo-wall-node and photo-wall-node-display abi.json) and OUTPUT the image's top
# directory, which holds ROOT as rootfs/ and gets environment.json, dependency-lock.json and
# sources.json beside it. Run by debian-packaging/build-root.sh, once mmdebstrap has finished:
# mmdebstrap's own cleanup (the apt caches and lists, /tmp, its apt configuration, the mount
# points' modes) runs after its customize hooks, so a seal run as one would inventory a tree the
# image does not hold.
#
# It changes no package file's bytes. It strips setid bits and group and other write from files
# (the inventory refuses them); writes an empty /etc/hostname (mmdebstrap copies the build host's,
# which differs between two builds); leaves dev, proc, sys, run and tmp empty, with today's
# mount points (run/photo-wall-client, run/photo-wall-wayland) and the empty 0444
# etc/photo-wall/public.json the Node binds the boot's public configuration over; drops the
# local repo's apt source (the snapshot pin's stays); removes every __pycache__ directory, so no
# .pyc lands in the RAM root (programs run `python3 -I -B`); then runs scripts/seal_root.py,
# which writes the entry, normalises directory modes and writes the metadata.
set -eu

[ $# -eq 5 ] || { echo "usage: $0 ROOT ROLE DEB ABI OUTPUT" >&2; exit 2; }
root=$1 role=$2 deb=$3 abi=$4 output=$5

find "$root" -xdev -type f -perm /6022 -exec chmod ug-s,go-w {} +
: > "$root/etc/hostname"
for directory in dev proc sys run tmp; do
	rm -rf "${root:?}/$directory"
	mkdir -m 0755 "$root/$directory"
done
mkdir -m 0755 "$root/run/photo-wall-client" "$root/run/photo-wall-wayland"
mkdir -p "$root/etc/photo-wall"
rm -f "$root/etc/photo-wall/public.json"
: > "$root/etc/photo-wall/public.json"
chmod 0444 "$root/etc/photo-wall/public.json"
rm -f "$root/etc/apt/sources.list.d/0001photo-wall-local.list"
find "$root" -xdev -type d -name __pycache__ -prune -exec rm -rf {} +

here=$(cd "$(dirname "$0")/.." && pwd)
exec python3 -I -B "$here/scripts/seal_root.py" --root "$root" --role "$role" --deb "$deb" \
	--abi "$abi/base.json" "$abi/display.json" --output "$output"
