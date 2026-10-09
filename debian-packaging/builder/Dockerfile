# The pinned Debian build container (decision 0019): Debian trixie, by image digest, with every
# package at the snapshot pin (debian-packaging/snapshot.list). debian-packaging/build-repo.sh
# builds it from the committed tree and runs dpkg-buildpackage and the local repo step in it.
#
#   pinned   the base image moved onto the pin. The image's own sources serve only the CA bundle
#            the https snapshot needs; then they go, and apt's preference holds the snapshot above
#            the image's newer packages, which dist-upgrade --allow-downgrades brings back to it.
#   builder  debhelper, dpkg-dev, curl (the repo step's one download) and the source package's
#            Build-Depends, read from debian/control.
FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f AS pinned
COPY debian-packaging/snapshot.list /etc/apt/sources.list.d/photo-wall-snapshot.list
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
 && rm /etc/apt/sources.list.d/debian.sources \
 && printf 'Package: *\nPin: origin snapshot.debian.org\nPin-Priority: 1001\n' \
    > /etc/apt/preferences.d/photo-wall-snapshot \
 && apt-get update && apt-get -y --allow-downgrades dist-upgrade \
 && rm -rf /var/lib/apt/lists/*

FROM pinned AS builder
COPY debian/control /src/debian/control
RUN apt-get update && apt-get install -y --no-install-recommends debhelper dpkg-dev curl \
 && apt-get build-dep -y /src \
 && rm -rf /var/lib/apt/lists/*
