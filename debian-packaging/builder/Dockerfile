# The pinned Debian build container (decision 0019): Debian trixie, by image digest, with every
# package at the snapshot pin (debian-packaging/snapshot.list). debian-packaging/build-repo.sh
# builds it from the committed tree and runs dpkg-buildpackage and the local repo step in it.
#
#   pinned   the base image moved onto the pin. The image's own sources serve only the CA bundle
#            the https snapshot needs; then they go, and apt's preference holds the snapshot above
#            the image's newer packages, which dist-upgrade --allow-downgrades brings back to it.
#   builder  debhelper, dpkg-dev, curl (the repo step's downloads) and the Build-Depends of both
#            source packages build-repo.sh builds, read from their control files: photo-wall's
#            (debian/control) and python-nats's (debian-packaging/python-nats/debian/control).
#   roots    mmdebstrap, squashfs-tools (mksquashfs, the image format's tool) and python3 (the seal)
#            at the pin: debian-packaging/build-root.sh builds and seals the release roots in it.
FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f AS pinned
COPY debian-packaging/snapshot.list /etc/apt/sources.list.d/photo-wall-snapshot.list
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
 && rm /etc/apt/sources.list.d/debian.sources \
 && printf 'Package: *\nPin: origin snapshot.debian.org\nPin-Priority: 1001\n' \
    > /etc/apt/preferences.d/photo-wall-snapshot \
 && apt-get update && apt-get -y --allow-downgrades dist-upgrade \
 && rm -rf /var/lib/apt/lists/*

FROM pinned AS builder
COPY debian/control /src/photo-wall/debian/control
COPY debian-packaging/python-nats/debian/control /src/python-nats/debian/control
RUN apt-get update && apt-get install -y --no-install-recommends debhelper dpkg-dev curl \
 && apt-get build-dep -y /src/photo-wall /src/python-nats \
 && rm -rf /var/lib/apt/lists/*

FROM pinned AS roots
RUN apt-get update && apt-get install -y --no-install-recommends mmdebstrap squashfs-tools python3 \
 && rm -rf /var/lib/apt/lists/*
