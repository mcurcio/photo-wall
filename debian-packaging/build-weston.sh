#!/usr/bin/env bash
# Build Photo Wall's Weston: Debian's own weston source package at the snapshot pin with
# debian-packaging/weston/patches/ added to its quilt series (docs/module-debian-packaging.md,
# "Weston").
#
#   debian-packaging/build-weston.sh --output DIR [--revision REV]
#
# Everything comes from the committed tree at REV (default HEAD), never the working copy:
#
#   1. the weston build container, photo-wall-debian-weston (build-container.sh --target weston),
#      which holds the pinned .dsc and its tarballs, checked in its own build
#      (debian-packaging/builder/Dockerfile), and Weston's Build-Depends from the pin;
#   2. in it, with no network: `dpkg-source -x`, the patches appended to Debian's series, a
#      changelog entry at WESTON_LOCAL_VERSION (debian-packaging/weston/upstream.env) dated at
#      the snapshot's instant (debian-packaging/snapshot-epoch.sh), the patches applied
#      (`dpkg-source --before-build`), scripts/weston_patch_check.py over the patched tree (the
#      build is refused while any plane mask can alias), then `dpkg-buildpackage -b` with
#      Debian's own debian/rules, its test suite included.
#
# DIR must not exist. It ends holding weston, libweston-14-0 and libweston-14-dev at
# WESTON_LOCAL_VERSION: build-repo.sh installs all three in its build root and puts all three in
# the local repo. The packages are arm64 (the Node's architecture) whatever the host is.
#
# The container's builder and BuildKit caches are build-container.sh's; CI keeps DIR itself in a
# cache keyed by these inputs (.github/workflows/node-components.yml, the `weston` job).
set -euo pipefail

ARCHITECTURE=arm64

usage() {
	echo "usage: $0 --output DIR [--revision REV]" >&2
	exit 2
}

output=
revision=HEAD
while [ $# -gt 0 ]; do
	case "$1" in
		--output) [ $# -ge 2 ] || usage; output=$2; shift 2 ;;
		--revision) [ $# -ge 2 ] || usage; revision=$2; shift 2 ;;
		*) usage ;;
	esac
done
[ -n "$output" ] || usage
if [ -e "$output" ]; then
	echo "build-weston: $output exists" >&2
	exit 1
fi

root=$(cd "$(dirname "$0")/.." && pwd)
commit=$(git -C "$root" rev-parse --verify "$revision^{commit}")

# 1. The weston container, from REV's own Dockerfile and pins.
WESTON_IMAGE=$("$root/debian-packaging/build-container.sh" --revision "$commit" --target weston)

mkdir -p "$output"
output=$(cd "$output" && pwd)

# 2. The patched build, with no network, as the caller under a name the container's passwd holds:
#    Weston's Xwayland test starts an X server that admits only a named local user. The
#    container starts as root only to add that entry, as build-repo.sh's package build does to
#    install.
build='
	mkdir -p /tmp/tree /tmp/build
	tar -x -C /tmp/tree
	pins=/tmp/tree/debian-packaging
	. "$pins/weston/upstream.env"
	cd /tmp/build
	dpkg-source -x "/src/weston/weston_$WESTON_VERSION.dsc" weston
	cd weston
	mkdir -p debian/patches
	while IFS= read -r patch; do
		cp "$pins/weston/patches/$patch" debian/patches/
		echo "$patch" >> debian/patches/series
	done < "$pins/weston/patches/series"
	epoch=$(sh "$pins/snapshot-epoch.sh" "$pins/snapshot.list")
	{
		printf "weston (%s) trixie; urgency=medium\n\n" "$WESTON_LOCAL_VERSION"
		sed -n "s/^Subject: /  * /p" "$pins"/weston/patches/*.patch
		printf "\n -- Photo Wall <noreply@example.invalid>  %s\n\n" "$(date -u -R -d "@$epoch")"
		cat debian/changelog
	} > /tmp/changelog
	mv /tmp/changelog debian/changelog
	dpkg-source --before-build .
	python3 -I -B /tmp/tree/scripts/weston_patch_check.py .
	DEB_BUILD_OPTIONS="noddebs parallel=$(nproc)" dpkg-buildpackage -b -us -uc
	for package in weston libweston-14-0 libweston-14-dev; do
		cp "../${package}_${WESTON_LOCAL_VERSION}_arm64.deb" /out/
	done'
git -C "$root" archive --format=tar "$commit" debian-packaging/weston \
	debian-packaging/snapshot.list debian-packaging/snapshot-epoch.sh scripts/weston_patch_check.py |
	docker run --rm -i --platform "linux/$ARCHITECTURE" --network none --env HOME=/tmp \
		--volume "$output:/out" "$WESTON_IMAGE" sh -ec '
	getent passwd "$1" >/dev/null || echo "builder:x:$1:$2::/tmp:/bin/sh" >> /etc/passwd
	getent group "$2" >/dev/null || echo "builder:x:$2:" >> /etc/group
	exec setpriv --reuid="$1" --regid="$2" --clear-groups sh -ec "$3"' \
		sh "$(id -u)" "$(id -g)" "$build"
echo "$output"
