#!/usr/bin/env bash
# Build the Node's Debian packages and their local repo (decision 0019, data flow step 1).
#
#   debian-packaging/build-repo.sh --output DIR [--revision REV]
#
# Everything comes from the committed tree at REV (default HEAD), never the working copy, so an
# uncommitted edit cannot reach a package:
#
#   1. the pinned build container, photo-wall-debian-builder, loaded by
#      debian-packaging/build-container.sh from REV's debian-packaging/ and debian/control;
#   2. `dpkg-buildpackage -b` over REV's whole tree in that container, with no network: every
#      binary package of debian/control, each at its content-derived version;
#   3. the local repo: upstream's arm64 nats-server .deb at the pin
#      (debian-packaging/nats-server.env), refused unless its sha256 is the pinned one, and the
#      apt index of all of them, Packages.
#
# DIR must not exist. It ends holding the .debs and Packages: a flat repo an image build reads as
# `deb [trusted=yes] file:<DIR> ./`. The packages are arm64 (the Node's architecture) whatever
# the host is.
#
# The container's builder and BuildKit caches are build-container.sh's (PHOTO_WALL_NODE_BUILDER,
# PHOTO_WALL_NODE_BUILD_CACHE_FROM and _TO).
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
	echo "build-repo: $output exists" >&2
	exit 1
fi

root=$(cd "$(dirname "$0")/.." && pwd)
commit=$(git -C "$root" rev-parse --verify "$revision^{commit}")

# 1. The build container, from REV's own Dockerfile, pin and control file.
BUILDER_IMAGE=$("$root/debian-packaging/build-container.sh" --revision "$commit")

mkdir -p "$output"
output=$(cd "$output" && pwd)
run() {
	docker run --rm -i --platform "linux/$ARCHITECTURE" --user "$(id -u):$(id -g)" \
		--env HOME=/tmp --volume "$output:/out" "$@"
}

# 2. The binary packages: the whole committed tree, built at a fixed path with no network.
git -C "$root" archive --format=tar "$commit" | run --network none "$BUILDER_IMAGE" sh -ec '
	mkdir -p /tmp/build/photo-wall
	tar -x -C /tmp/build/photo-wall
	cd /tmp/build/photo-wall
	dpkg-buildpackage -b -us -uc
	cp ../*.deb /out/'

# 3. The local repo: the pinned nats-server .deb beside them, and the index.
git -C "$root" show "$commit:debian-packaging/nats-server.env" | run "$BUILDER_IMAGE" sh -ec '
	cat > /tmp/nats-server.env
	. /tmp/nats-server.env
	cd /out
	deb="nats-server_${NATS_SERVER_VERSION}_arm64.deb"
	curl -fsSL --retry 3 -o "$deb" \
		"https://github.com/nats-io/nats-server/releases/download/v$NATS_SERVER_VERSION/nats-server-v$NATS_SERVER_VERSION-arm64.deb"
	echo "$NATS_SERVER_ARM64_DEB_SHA256  $deb" | sha256sum -c -
	dpkg-scanpackages --multiversion . > Packages'
echo "$output"
