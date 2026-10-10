# The real-PID1 scenarios' image (tests/test_node_pid1.py), built by tests/node_pid1_fixture/build.sh.
#
# FROM the pinned Debian build container (photo-wall-debian-builder, as loaded): the build root
# of the packages, which holds the pinned Weston stack, python3-cffi-backend (the base's
# rpi-image-gen layer gives the display it) and the compiler the fixture head needs. It installs
# photo-wall-node by name from the local repo, read as the display harness reads it (a `file:`
# source preferred at 1002), with every context it pins at its exact version and upstream's
# nats-server, and compiles the checked-in headless fixture head. The repo stays at
# /var/tmp/photo-wall-debs for the scenarios' package binding (tests/node_pid1_package_verify.py).
ARG BASE
FROM ${BASE}
COPY fixture-head.c /var/tmp/
COPY debs /var/tmp/photo-wall-debs
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod 755 /usr/sbin/policy-rc.d \
 && echo 'deb [trusted=yes] file:/var/tmp/photo-wall-debs ./' > /etc/apt/sources.list.d/photo-wall-local.list \
 && printf 'Package: *\nPin: origin ""\nPin-Priority: 1002\n' > /etc/apt/preferences.d/photo-wall-local \
 && apt-get update \
 && apt-get install -y --no-install-recommends photo-wall-node \
 && dpkg --audit
RUN cc -shared -fPIC -Wall -Wextra -Werror $(pkg-config --cflags libweston-14) \
 /var/tmp/fixture-head.c -o /var/tmp/fixture-head.so $(pkg-config --libs libweston-14)
RUN mkdir -p /etc/photo-wall /var/lib/node-fixture-drm/card0-Virtual-1 \
 && printf 'connected\n' > /var/lib/node-fixture-drm/card0-Virtual-1/status
