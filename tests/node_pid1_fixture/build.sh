#!/usr/bin/env bash
# Build the real-PID1 scenarios' fixture (tests/test_node_pid1.py) from one node component set.
#
#   tests/node_pid1_fixture/build.sh --repo DIR --components DIR --output DIR [--revision REV]
#
# --repo is the local repo (debian-packaging/build-repo.sh), --components the release writer's
# set built from it (scripts/node_release_writer.py). OUTPUT, a new directory, gets:
#
#   targets/{success,failure}.squashfs and -reference.json
#       Nonrelease stage targets, release roots like the app's, built by
#       debian-packaging/build-root.sh from the repo with its photo-wall-player replaced, and so
#       sealed for the same ABI: `success` is the Player itself at a higher version; `failure` is
#       an equivs stub photo-wall-player whose entry exits. Each reference is the seal's plus its
#       image's sha256 and size.
#   fixture.json
#       {"components": <dir>, "image": <sha256 image ID>, "base_image": <its FROM>} for
#       tests/test_node_pid1.py (PHOTO_WALL_NODE_PID1_FIXTURE names OUTPUT).
#
# The image is tests/node_pid1_fixture/Dockerfile, FROM the pinned Debian build container
# (debian-packaging/build-container.sh), as just loaded, with the repo and the fixture head as its
# context, built by `docker build` (a local base image needs the daemon's own builder).
set -euo pipefail

ARCHITECTURE=arm64

usage() {
	echo "usage: $0 --repo DIR --components DIR --output DIR [--revision REV]" >&2
	exit 2
}

repo= components= output= revision=HEAD
while [ $# -gt 0 ]; do
	case "$1" in
		--repo) [ $# -ge 2 ] || usage; repo=$2; shift 2 ;;
		--components) [ $# -ge 2 ] || usage; components=$2; shift 2 ;;
		--output) [ $# -ge 2 ] || usage; output=$2; shift 2 ;;
		--revision) [ $# -ge 2 ] || usage; revision=$2; shift 2 ;;
		*) usage ;;
	esac
done
[ -n "$repo" ] && [ -n "$components" ] && [ -n "$output" ] || usage
if [ -e "$output" ]; then
	echo "node-pid1-fixture: $output exists" >&2
	exit 1
fi

root=$(cd "$(dirname "$0")/../.." && pwd)
commit=$(git -C "$root" rev-parse --verify "$revision^{commit}")
repo=$(cd "$repo" && pwd)
components=$(cd "$components" && pwd)
builder=$("$root/debian-packaging/build-container.sh" --revision "$commit")
base=$(docker image inspect --format '{{.Id}}' "$builder")

mkdir -p "$output/targets"
output=$(cd "$output" && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/node-pid1-fixture.XXXXXX")
trap 'rm -rf "$work"' EXIT

# The two target repos: the local repo with its photo-wall-player replaced, and their indexes.
# `success` is the built Player's own bytes at another version, so its sealed image differs from
# the release's app root while running the real program. The source package cannot give it: a
# version there is a hash of the installed tree (debian/content-versions), so the same bytes
# always rebuild at the release's version. dpkg-deb's own unpack and build re-version it; nothing
# else of the package changes. `failure` needs no real bytes, so it is an equivs stub.
for role in success failure; do
	mkdir "$work/$role-repo"
	find "$repo" -maxdepth 1 -name '*.deb' ! -name 'photo-wall-player_*' -exec cp {} "$work/$role-repo/" \;
done
docker run --rm -i --platform "linux/$ARCHITECTURE" --volume "$repo:/repo:ro" \
	--volume "$root/debian-packaging/repo-file.sh:/repo-file.sh:ro" \
	--volume "$work:/work" "$base" sh -ec '
	player=$(/repo-file.sh /repo photo-wall-player)
	version=$(dpkg-deb --field "$player" Version)
	dpkg-deb -R "$player" /tmp/success
	sed -i "s/^Version: .*/Version: $version+success/" /tmp/success/DEBIAN/control
	dpkg-deb --build --root-owner-group /tmp/success /work/success-repo/ >/dev/null
	apt-get -qq update && apt-get -qq install -y --no-install-recommends equivs >/dev/null
	mkdir /tmp/failure && cd /tmp/failure
	echo "raise SystemExit(\"intentional nonrelease PID1 failure fixture\")" > __main__.py
	cat > control <<-EOF
	Package: photo-wall-player
	Version: $version+failure
	Architecture: all
	Depends: python3
	Files: __main__.py /usr/lib/photo-wall/player/
	Description: The PID1 scenarios failure target
	 A Player whose entry exits.
	EOF
	equivs-build control >/dev/null
	cp photo-wall-player_*.deb /work/failure-repo/
	for role in success failure; do
		(cd "/work/$role-repo" && dpkg-scanpackages --multiversion . > Packages 2>/dev/null)
	done
	chown -R "$1" /work' sh "$(id -u):$(id -g)"

for role in success failure; do
	"$root/debian-packaging/build-root.sh" --once --revision "$commit" --repo "$work/$role-repo" \
		--package photo-wall-player --output "$work/$role.squashfs" >/dev/null
	mv "$work/$role.squashfs" "$output/targets/$role.squashfs"
	python3 - "$work/$role.reference.json" "$output/targets/$role.squashfs" \
		"$output/targets/$role-reference.json" <<-'EOF'
	import hashlib, json, os, sys
	reference = json.load(open(sys.argv[1]))
	with open(sys.argv[2], "rb") as image:
	    reference["environment_sha256"] = hashlib.sha256(image.read()).hexdigest()
	reference["size_bytes"] = os.path.getsize(sys.argv[2])
	json.dump(reference, open(sys.argv[3], "w"), sort_keys=True)
	EOF
done

mkdir "$work/image"
cp -R "$repo" "$work/image/debs"
cp "$root/tests/node_pid1_fixture_head.c" "$work/image/fixture-head.c"
docker build --quiet --platform "linux/$ARCHITECTURE" --build-arg BASE="$builder" \
	--iidfile "$work/image-id" --file "$root/tests/node_pid1_fixture/Dockerfile" "$work/image" >/dev/null
image=$(cat "$work/image-id")
printf '{"base_image": "%s", "components": "%s", "image": "%s"}\n' "$base" "$components" "$image" \
	> "$output/fixture.json"
echo "$output"
