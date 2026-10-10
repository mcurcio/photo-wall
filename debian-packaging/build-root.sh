#!/usr/bin/env bash
# Build one release root's squashfs image from the snapshot pin and the local repo (decision 0019,
# data flow step 3).
#
#   debian-packaging/build-root.sh --repo DIR --package NAME --output FILE.squashfs [--once] [--revision REV]
#   debian-packaging/build-root.sh --repo DIR --package NAME --resolve [--revision REV]
#
# DIR is a debian-packaging/build-repo.sh output, NAME a root package: photo-wall-player (the
# app root) or photo-wall-app-manager (the manager root). Everything else comes from the
# committed tree at REV (default HEAD), never the working copy: the roots container
# (debian-packaging/build-container.sh --target roots: mmdebstrap and squashfs-tools at the pin),
# debian-packaging/snapshot.list and its epoch (debian-packaging/snapshot-epoch.sh), the image
# format (debian-packaging/image-format.env), this recipe, debian-packaging/seal-hook.sh and
# scripts/seal_root.py.
#
# A build runs, in a privileged roots container:
#
#   SOURCE_DATE_EPOCH=<the pin's> mmdebstrap --mode=root --variant=apt --arch=arm64
#       --skip=output/dev, docs (but copyrights), man pages and locales path-excluded,
#       --include=NAME trixie <rootfs> <snapshot.list> <the local repo, copied in by apt>
#   debian-packaging/seal-hook.sh <rootfs> <role> <NAME's .deb> <the repo's abi.json pair> <image>
#   mksquashfs <image> <out> $SQUASHFS_OPTIONS
#
# twice, at once, in two containers (whose hostnames differ), and refuses two different image digests:
# a release root must keep its digest when it is rebuilt. --once builds once, for a root no
# release ships (the PID1 scenarios' stage targets, tests/node_pid1_fixture/build.sh).
# FILE then holds the image; FILE less .squashfs plus .reference.json holds the seal's reference
# (scripts/seal_root.py), for the release writer. The caller builds only on a cache miss.
#
# --resolve prints, sorted, the Package=Version lines mmdebstrap's apt would install for NAME
# (a simulated run): the root's input, for node-components' cache key.
set -euo pipefail

ARCHITECTURE=arm64

usage() {
	echo "usage: $0 --repo DIR --package NAME (--output FILE.squashfs [--once] | --resolve) [--revision REV]" >&2
	exit 2
}

repo= package= output= resolve= once= revision=HEAD
while [ $# -gt 0 ]; do
	case "$1" in
		--repo) [ $# -ge 2 ] || usage; repo=$2; shift 2 ;;
		--package) [ $# -ge 2 ] || usage; package=$2; shift 2 ;;
		--output) [ $# -ge 2 ] || usage; output=$2; shift 2 ;;
		--resolve) resolve=yes; shift ;;
		--once) once=yes; shift ;;
		--revision) [ $# -ge 2 ] || usage; revision=$2; shift 2 ;;
		*) usage ;;
	esac
done
[ -n "$repo" ] && [ -n "$package" ] || usage
[ -n "$output$resolve" ] && { [ -z "$output" ] || [ -z "$resolve" ]; } || usage
case "$package" in
	photo-wall-player) role=app ;;
	photo-wall-app-manager) role=manager-primary ;;
	*) echo "build-root: $package is no root package" >&2; exit 2 ;;
esac

root=$(cd "$(dirname "$0")/.." && pwd)
commit=$(git -C "$root" rev-parse --verify "$revision^{commit}")
repo=$(cd "$repo" && pwd)
ROOTS_IMAGE=$("$root/debian-packaging/build-container.sh" --revision "$commit" --target roots)

work=$(mktemp -d "${TMPDIR:-/tmp}/build-root.XXXXXX")
trap 'rm -rf "$work"' EXIT
mkdir "$work/src"
git -C "$root" archive --format=tar "$commit" | tar -x -C "$work/src"

# The common mmdebstrap arguments (positional $@ of the in-container scripts).
MMDEBSTRAP='
	echo "deb [trusted=yes] copy:/repo ./" > /tmp/photo-wall-local.list
	mkdir -m 0755 /tmp/image
	set -- --mode=root --variant=apt --arch=arm64 \
		--aptopt="Acquire::Check-Valid-Until \"false\"" --include="$package" "$@" \
		trixie /tmp/image/rootfs /src/debian-packaging/snapshot.list /tmp/photo-wall-local.list'

roots() {
	docker run --rm -i --privileged --platform "linux/$ARCHITECTURE" \
		--volume "$repo:/repo:ro" --volume "$work/src:/src:ro" "$@"
}

if [ -n "$resolve" ]; then
	roots --env package="$package" "$ROOTS_IMAGE" sh -ec "$MMDEBSTRAP"'
		mmdebstrap --simulate --verbose "$@" > /tmp/simulated 2>&1 || { cat /tmp/simulated >&2; exit 1; }
		sed -n "s/^Inst \([^ ]*\) (\([^ ]*\) .*/\1=\2/p" /tmp/simulated | LC_ALL=C sort -u'
	exit 0
fi

case "$output" in *.squashfs) ;; *) usage ;; esac
if [ -e "$output" ]; then
	echo "build-root: $output exists" >&2
	exit 1
fi

build() {
	mkdir "$work/$1"
	roots --volume "$work/$1:/out" --env package="$package" --env role="$role" \
		--env owner="$(id -u):$(id -g)" "$ROOTS_IMAGE" sh -ec "$MMDEBSTRAP"'
		set -a; . /src/debian-packaging/image-format.env; set +a
		SOURCE_DATE_EPOCH=$(sh /src/debian-packaging/snapshot-epoch.sh)
		export SOURCE_DATE_EPOCH
		deb() { /src/debian-packaging/repo-file.sh /repo "$1"; }
		mkdir /tmp/abi
		dpkg-deb --fsys-tarfile "$(deb photo-wall-node)" |
			tar -xO ./usr/lib/photo-wall/node/abi.json > /tmp/abi/base.json
		dpkg-deb --fsys-tarfile "$(deb photo-wall-node-display)" |
			tar -xO ./usr/lib/photo-wall/node-display/abi.json > /tmp/abi/display.json
		mmdebstrap --quiet "$@" --skip=output/dev \
			--dpkgopt="path-exclude=/usr/share/doc/*" \
			--dpkgopt="path-include=/usr/share/doc/*/copyright" \
			--dpkgopt="path-exclude=/usr/share/man/*" \
			--dpkgopt="path-exclude=/usr/share/locale/*"
		/src/debian-packaging/seal-hook.sh /tmp/image/rootfs "$role" "$(deb "$package")" /tmp/abi /tmp/image
		# shellcheck disable=SC2086 # the options are a word list
		mksquashfs /tmp/image /out/root.squashfs $SQUASHFS_OPTIONS >/dev/null
		cp /tmp/reference.json /out/
		chown "$owner" /out/root.squashfs /out/reference.json'
}

# The two builds are independent containers, so they run at once: a rebuild costs one build's
# wall time. Both are waited for, whichever fails, before a failed one stops the script.
build first &
pids=$!
if [ -z "$once" ]; then
	build second &
	pids="$pids $!"
fi
status=0
for pid in $pids; do
	wait "$pid" || status=$?
done
[ "$status" -eq 0 ] || exit "$status"
if [ -z "$once" ]; then
	first=$(sha256sum "$work/first/root.squashfs" | cut -d" " -f1)
	second=$(sha256sum "$work/second/root.squashfs" | cut -d" " -f1)
	if [ "$first" != "$second" ]; then
		echo "build-root: two builds of $package gave different images ($first, $second)" >&2
		exit 1
	fi
fi
mv "$work/first/root.squashfs" "$output"
mv "$work/first/reference.json" "${output%.squashfs}.reference.json"
echo "$output"
