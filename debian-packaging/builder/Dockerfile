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
#   weston   Debian's weston source package at the pin (debian-packaging/weston/upstream.env),
#            refused unless it is the weston apt's candidate at the pin (archive and security
#            suite), so a snapshot bump that brings a new Debian Weston, a security update
#            included, stops the build until WESTON_VERSION and the patches move with it:
#            its .dsc from the snapshot's archive, refused unless its sha256 is the pinned one, the
#            tarballs it names (dpkg-source -x checks them against it) and its Build-Depends, in
#            /src/weston: debian-packaging/build-weston.sh builds it, patched, with no network.
#            /tmp/.X11-unix is made as a Debian build chroot has it (root's, mode 1777): Weston's
#            Xwayland test cannot start an X server in the one debian/rules makes as the build
#            user.
#            Apart from the builder, so Weston's large build closure stays out of every other
#            build.
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

FROM pinned AS weston
COPY debian-packaging/weston/upstream.env /src/weston/upstream.env
WORKDIR /src/weston
RUN . ./upstream.env \
 && archive=$(sed -n '/^deb /{s|^deb .*\(https://snapshot\.debian\.org/archive/debian/[0-9TZ]*\) .*$|\1|p;q;}' \
      /etc/apt/sources.list.d/photo-wall-snapshot.list) \
 && pool="$archive/pool/main/w/weston" dsc="weston_$WESTON_VERSION.dsc" \
 && apt-get update \
 && candidate=$(apt-cache policy weston | sed -n 's/^  Candidate: //p') \
 && if [ "$candidate" != "$WESTON_VERSION" ]; then \
      echo "weston: the pin's weston is $candidate, not WESTON_VERSION $WESTON_VERSION:" \
        "move debian-packaging/weston/upstream.env to it and re-check the patches" >&2; \
      exit 1; fi \
 && apt-get install -y --no-install-recommends debhelper dpkg-dev curl python3 xz-utils \
 && curl -fsSL --retry 3 -o "$dsc" "$pool/$dsc" \
 && echo "$WESTON_DSC_SHA256  $dsc" | sha256sum -c - \
 && for file in $(sed -n '/^Checksums-Sha256:/,/^[^ ]/s/^ [0-9a-f]\{64\} [0-9]* //p' "$dsc"); do \
      curl -fsSL --retry 3 -o "$file" "$pool/$file"; done \
 && apt-get build-dep -y --no-install-recommends "./$dsc" \
 && rm -rf /var/lib/apt/lists/* \
 && install -d -m 1777 /tmp/.X11-unix
WORKDIR /
