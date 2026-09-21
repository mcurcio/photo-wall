#!/bin/sh
# Media-worker container entrypoint: let the platform own storage identity.
#
# On shared-storage platforms the worker is handed a media root it does not
# own -- a Kubernetes NFS PVC root is a pre-existing export owned by uid 0,
# mounted over the image's baked wall:wall 0700 path. The worker (uid 10001)
# then cannot chmod/create under its own root and crashes at startup with
# `media_io` (central/media_store.py: chmod + .worker.lock on a root it does
# not own). See docs/module-media-worker.md.
#
# ONLY when the container is started as root does this entrypoint materialize
# the single cache root's domain subdirs (media/ apps/ os-images/) owned by a
# configurable uid/gid, then drop to that uid via gosu before exec'ing the real
# command. A platform expresses the desired identity with
# PHOTO_WALL_PUID/PHOTO_WALL_PGID and starts the container as root (a
# securityContext concern, not a compose one). The worker is the single writer,
# so this is the one place the runtime cache layout is created.
#
# Compose runs the image as the baked non-root `USER wall` (uid 10001) with
# cap_drop:[ALL] + no-new-privileges:true. There `id -u` is not 0, so the root
# branch is skipped entirely and the command execs directly -- no chown, no
# gosu, no new privilege -- byte-for-byte the prior behaviour.
set -eu

PUID="${PHOTO_WALL_PUID:-10001}"
PGID="${PHOTO_WALL_PGID:-10001}"

if [ "$(id -u)" = "0" ]; then
    # Fail fast on a misconfigured target identity. A non-numeric, zero, or
    # leading-zero uid/gid would otherwise chown the storage to an unintended
    # owner or leave the worker running as root/wrong uid, so refuse it loudly
    # (EX_CONFIG, 78) before any chown or privilege drop rather than silently
    # proceeding. The reject-pattern `''|*[!0-9]*|0|0?*` makes canonical positive
    # decimal a construction property in ONE `case`: it rejects empty (`''`), any
    # non-digit (`*[!0-9]*`), bare zero (`0`), and any leading-zero multi-digit
    # spelling (`0?*`, i.e. `00`/`000`/`010`/`007`) -- accepting only `[1-9][0-9]*`.
    # There is no octal/leading-zero identity to misread, and no separate
    # arithmetic step to reason about under `set -e`.
    case "$PUID" in
        ''|*[!0-9]*|0|0?*)
            echo "docker-entrypoint: PHOTO_WALL_PUID must be a positive integer, got '${PUID}'" >&2
            exit 78 ;;
    esac
    case "$PGID" in
        ''|*[!0-9]*|0|0?*)
            echo "docker-entrypoint: PHOTO_WALL_PGID must be a positive integer, got '${PGID}'" >&2
            exit 78 ;;
    esac
    # Create + own each domain subdir atomically. `install -d` is non-recursive
    # by intent: the worker is the single writer and owns the files it creates,
    # so there is nothing under these dirs to chown. A Kubernetes PVC root is
    # handed to us bare and root-owned; this is the step that makes it writable
    # for PUID before the privilege drop.
    CACHE_ROOT="${PHOTO_WALL_CACHE_ROOT:-/var/cache/photo-wall}"
    for sub in media apps os-images; do
        install -d -o "$PUID" -g "$PGID" -m 0700 "$CACHE_ROOT/$sub"
    done
    exec gosu "$PUID:$PGID" "$@"
fi

exec "$@"
