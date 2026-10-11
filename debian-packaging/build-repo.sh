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
#   2. python3-nats: upstream's nats-py sdist at the pin
#      (debian-packaging/python-nats/upstream.env), fetched in a networked step and refused unless
#      its sha256 is the pinned one, placed as the .orig.tar.gz of the python-nats source package
#      (debian-packaging/python-nats/debian) and built by `dpkg-buildpackage -b` with no network;
#   3. `dpkg-buildpackage -b` over REV's whole tree in that container, with no network: every
#      binary package of debian/control, each at its content-derived version. Step 2's
#      python3-nats is installed into this build's container first (`dpkg -i`, as root; the build
#      itself runs as the caller), so the build root holds it like every other package a Photo
#      Wall package Depends on (decision 0019's import check resolves each import to its owning
#      package there). It is this run's own product, so it is no Build-Depends: the container
#      takes those from the pin alone;
#   4. the local repo: upstream's arm64 nats-server .deb at the pin
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

# 2. python3-nats: the sdist, fetched into a private directory beside the output (the one
#    networked step), then its source package built from it with no network.
sdist=$(mktemp -d "$output.sdist.XXXXXX")
trap 'rm -rf "$sdist"' EXIT
git -C "$root" show "$commit:debian-packaging/python-nats/upstream.env" |
	run --volume "$sdist:/sdist" "$BUILDER_IMAGE" sh -ec '
	cat > /tmp/upstream.env
	. /tmp/upstream.env
	orig="/sdist/python-nats_$NATS_PY_VERSION.orig.tar.gz"
	curl -fsSL --retry 3 -o "$orig" "$NATS_PY_SDIST_URL"
	echo "$NATS_PY_SDIST_SHA256  $orig" | sha256sum -c -'
git -C "$root" archive --format=tar "$commit" debian-packaging/python-nats |
	run --network none --volume "$sdist:/sdist:ro" "$BUILDER_IMAGE" sh -ec '
	mkdir -p /tmp/build/python-nats /tmp/tree
	tar -x -C /tmp/tree
	cp /sdist/*.orig.tar.gz /tmp/build/
	tar -xz -C /tmp/build/python-nats --strip-components=1 -f /tmp/build/*.orig.tar.gz
	cp -R /tmp/tree/debian-packaging/python-nats/debian /tmp/build/python-nats/
	cd /tmp/build/python-nats
	dpkg-buildpackage -b -us -uc
	cp ../*.deb /out/'

# 3. The binary packages: the whole committed tree, built at a fixed path with no network, in a
#    container that runs as root only to install step 2's python3-nats.
git -C "$root" archive --format=tar "$commit" |
	docker run --rm -i --platform "linux/$ARCHITECTURE" --network none --env HOME=/tmp \
		--volume "$output:/out" "$BUILDER_IMAGE" sh -ec '
	dpkg -i /out/python3-nats_*.deb >/dev/null
	exec setpriv --reuid="$1" --regid="$2" --clear-groups sh -ec "
		mkdir -p /tmp/build/photo-wall
		tar -x -C /tmp/build/photo-wall
		cd /tmp/build/photo-wall
		dpkg-buildpackage -b -us -uc
		cp ../*.deb /out/"' sh "$(id -u)" "$(id -g)"

# 4. The local repo: the pinned nats-server .deb beside them, and the index.
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
