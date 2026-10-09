#!/usr/bin/env bash
# Load the pinned Debian build container (decision 0019) and print its tag.
#
#   debian-packaging/build-container.sh [--revision REV]
#
# The container is debian-packaging/builder/Dockerfile's target `builder`, loaded as
# photo-wall-debian-builder for linux/arm64 (the Node's architecture) and built from REV's
# committed debian-packaging/ and debian/control (default HEAD), never the working copy. Its
# users: debian-packaging/build-repo.sh (dpkg-buildpackage and the local repo step), the PID1
# fixture image (FROM it) and the display harness image (FROM it), and tests/debs (installs).
#
# It is built by `docker buildx build` on the builder PHOTO_WALL_NODE_BUILDER names (Docker's
# default builder when unset), reading and writing the BuildKit caches
# PHOTO_WALL_NODE_BUILD_CACHE_FROM and _TO name, with `{role}` replaced by `debian-builder`: the
# node build cache policy of scripts/node_build_inputs.py, which changes no byte. The build's
# own output goes to stderr; stdout is the tag alone.
set -euo pipefail

ARCHITECTURE=arm64
BUILDER_IMAGE=photo-wall-debian-builder
ROLE=debian-builder

usage() {
	echo "usage: $0 [--revision REV]" >&2
	exit 2
}

revision=HEAD
while [ $# -gt 0 ]; do
	case "$1" in
		--revision) [ $# -ge 2 ] || usage; revision=$2; shift 2 ;;
		*) usage ;;
	esac
done

root=$(cd "$(dirname "$0")/.." && pwd)
commit=$(git -C "$root" rev-parse --verify "$revision^{commit}")

set -- --builder "${PHOTO_WALL_NODE_BUILDER:-default}" --load --platform "linux/$ARCHITECTURE"
for pair in "cache-from:${PHOTO_WALL_NODE_BUILD_CACHE_FROM:-}" "cache-to:${PHOTO_WALL_NODE_BUILD_CACHE_TO:-}"; do
	value=${pair#*:}
	[ -n "$value" ] || continue
	if [ -z "${PHOTO_WALL_NODE_BUILDER:-}" ]; then
		echo "build-container: a BuildKit cache needs PHOTO_WALL_NODE_BUILDER" >&2
		exit 1
	fi
	set -- "$@" "--${pair%%:*}" "$(printf '%s' "$value" | sed "s/{role}/$ROLE/g")"
done
git -C "$root" archive --format=tar "$commit" debian-packaging debian/control |
	docker buildx build "$@" --target builder --tag "$BUILDER_IMAGE" \
		--file debian-packaging/builder/Dockerfile - >&2
echo "$BUILDER_IMAGE"
