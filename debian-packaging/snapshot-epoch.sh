#!/bin/sh
# Print the snapshot pin's SOURCE_DATE_EPOCH: the instant its first source names, in seconds.
#
#   debian-packaging/snapshot-epoch.sh [SNAPSHOT_LIST]
#
# SNAPSHOT_LIST defaults to the snapshot.list beside this script, the pin's one home (decision
# 0019, rule 1). Readers: debian-packaging/build-root.sh (the release roots' file times) and
# .github/workflows/base-image.yml (the base's, and the initrd verify's CA-age reference).
# POSIX sh with GNU date, the build hosts' and containers'.
set -eu

list=${1:-$(dirname "$0")/snapshot.list}
moment=$(sed -n '/^deb /{
s|^deb .*/archive/debian/\([0-9]\{8\}\)T\([0-9]\{2\}\)\([0-9]\{2\}\)\([0-9]\{2\}\)Z .*$|\1 \2:\3:\4|p
q
}' "$list")
if [ -z "$moment" ]; then
	echo "snapshot-epoch: $list: its first source names no snapshot.debian.org instant" >&2
	exit 1
fi
date -u -d "$moment" +%s
