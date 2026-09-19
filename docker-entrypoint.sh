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
# ONLY when the container is started as root does this entrypoint take
# ownership of the media (and optional app) roots for a configurable uid/gid
# and then drop to that uid via gosu before exec'ing the real command. A
# platform expresses the desired identity with PHOTO_WALL_PUID/PHOTO_WALL_PGID
# and starts the container as root (a securityContext concern, not a compose
# one).
#
# Compose runs the image as the baked non-root `USER wall` (uid 10001) with
# cap_drop:[ALL] + no-new-privileges:true. There `id -u` is not 0, so the root
# branch is skipped entirely and the command execs directly -- no chown, no
# gosu, no new privilege -- byte-for-byte the prior behaviour.
set -eu

PUID="${PHOTO_WALL_PUID:-10001}"
PGID="${PHOTO_WALL_PGID:-10001}"

if [ "$(id -u)" = "0" ]; then
    # Fail fast on a misconfigured target identity. A non-numeric or zero uid/gid
    # would otherwise chown the storage to an unintended owner or leave the
    # worker running as root, so refuse it loudly (EX_CONFIG, 78) before any
    # chown or privilege drop rather than silently proceeding.
    case "$PUID" in
        ''|*[!0-9]*|0)
            echo "docker-entrypoint: PHOTO_WALL_PUID must be a positive integer, got '${PUID}'" >&2
            exit 78 ;;
    esac
    case "$PGID" in
        ''|*[!0-9]*|0)
            echo "docker-entrypoint: PHOTO_WALL_PGID must be a positive integer, got '${PGID}'" >&2
            exit 78 ;;
    esac
    # An `A && B && chown` one-liner would abort the whole script under `set -e`
    # whenever the guard is false (e.g. PHOTO_WALL_APP_ROOT unset), so gate each
    # chown with an explicit `if` whose condition `set -e` does not police.
    for dir in \
        "${PHOTO_WALL_MEDIA_ROOT:-/var/lib/photo-wall/media}" \
        "${PHOTO_WALL_APP_ROOT:-}"
    do
        if [ -n "$dir" ] && [ -d "$dir" ]; then
            chown "$PUID:$PGID" "$dir"
        fi
    done
    exec gosu "$PUID:$PGID" "$@"
fi

exec "$@"
