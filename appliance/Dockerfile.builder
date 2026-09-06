# Isolated file-backed image tooling; no host devices or bind mounts are needed.
FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS public-trust
FROM ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517
ENV DEBIAN_FRONTEND=noninteractive LIBGUESTFS_BACKEND=direct LIBGUESTFS_BACKEND_SETTINGS=force_tcg
# Bare Ubuntu lacks the public CA bundle required by the HTTPS snapshot service.
COPY --from=public-trust /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
# APT also authenticates the snapshot index and every package hash.
RUN rm -f /etc/apt/sources.list.d/ubuntu.sources && \
    printf '%s\n' \
      'deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] https://snapshot.ubuntu.com/ubuntu/20260905T000000Z noble main universe restricted multiverse' \
      'deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] https://snapshot.ubuntu.com/ubuntu/20260905T000000Z noble-updates main universe restricted multiverse' \
      'deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] https://snapshot.ubuntu.com/ubuntu/20260905T000000Z noble-security main universe restricted multiverse' \
      > /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates python3.12 python3-guestfs python3-packaging python3-pip git gnupg initramfs-tools-core libguestfs-tools linux-image-generic \
      qemu-system-arm squashfs-tools e2fsprogs dosfstools mtools xz-utils \
      openssl attr acl tar && \
    dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n' > /tool-packages.tsv
RUN apt-get install -y --no-install-recommends python3-pytest && \
    dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n' > /tool-packages.tsv
# build_player.py is a host-side tool, and its locked source requires the
# exact Packaging version recorded in uv.lock rather than Noble's apt version.
COPY uv.lock /tmp/photo-wall-tools.lock
RUN python3.12 -c 'import tomllib; p=next(p for p in tomllib.load(open("/tmp/photo-wall-tools.lock","rb"))["package"] if p["name"]=="packaging"); w=next(w for w in p["wheels"] if w["url"].endswith("-py3-none-any.whl")); print("packaging @ " + w["url"] + " --hash=" + w["hash"])' > /tmp/photo-wall-build-tools.txt
RUN python3.12 -m pip install --break-system-packages --no-cache-dir --no-deps --require-hashes \
      -r /tmp/photo-wall-build-tools.txt && \
    python3.12 -c 'from packaging.markers import Marker; assert Marker("python_version >= \"3.12\"").evaluate(context="requirement")'
WORKDIR /work
CMD ["/bin/sh"]
