# The standalone native definition ends at the marker below. Application inputs
# must stay outside it so dependency updates cannot trigger another APT build.
ARG MEDIA_BASE_IMAGE=media-os
FROM python:3.12.11-slim-trixie@sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f AS python-system
WORKDIR /app
# 0013: identity is a build arg so a platform can pin PUID/PGID to its storage;
# the GID is pinned via groupadd so the leaf-stage `install -d -g` and the
# entrypoint's runtime chown resolve the same numeric group everywhere. The args
# are re-exported as runtime ENV so the entrypoint and leaf-stage RUNs inherit
# them without redeclaring the ARG.
ARG PHOTO_WALL_PUID=10001
ARG PHOTO_WALL_PGID=10001
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 \
    PHOTO_WALL_CACHE_ROOT=/var/cache/photo-wall \
    PHOTO_WALL_PUID=${PHOTO_WALL_PUID} \
    PHOTO_WALL_PGID=${PHOTO_WALL_PGID}
RUN groupadd --gid "$PHOTO_WALL_PGID" wall \
    && useradd --system --uid "$PHOTO_WALL_PUID" --gid "$PHOTO_WALL_PGID" --create-home wall \
    && install -d -o "$PHOTO_WALL_PUID" -g "$PHOTO_WALL_PGID" -m 0700 /etc/photo-wall/private

FROM python-system AS media-os
RUN rm -f /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' \
       'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20260905T000000Z trixie main' \
       'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/20260905T000000Z trixie-security main' \
       > /etc/apt/sources.list \
    && attempt=1 \
    && until apt-get -o Acquire::Retries=5 update \
          && apt-get -o Acquire::Retries=5 install -y --no-install-recommends ffmpeg gosu; do \
         if [ "$attempt" -ge 4 ]; then exit 1; fi; \
         sleep $((attempt * 15)); \
         attempt=$((attempt + 1)); \
       done \
    && dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n' > /etc/photo-wall/packages.tsv \
    && sha256sum /usr/bin/ffmpeg /usr/bin/ffprobe > /etc/photo-wall/conversion-binaries.sha256 \
    && printf '%s\n' '{"schema":1,"connections":[]}' > /etc/photo-wall/private/connections.json \
    && chown wall:wall /etc/photo-wall/private/connections.json \
    && chmod 0600 /etc/photo-wall/private/connections.json \
    && ffmpeg -version > /dev/null && ffprobe -version > /dev/null \
    && gosu nobody true \
    && rm -rf /var/lib/apt/lists/*

# END MEDIA OS DEFINITION

# Application layers (docs/module-appliance-ci.md, "Service image builds"). Each service target
# stacks independent layers, least to most often changed: its base, the locked third-party
# environment, the application source, then the project's own editable install. Each is
# `COPY --link`ed from the one stage that owns it, so its cache key is its own content: a code
# change rebuilds only the source and project-install layers, and a new media OS base rebases
# the worker's dependency and source layers instead of rebuilding them (its RUN steps re-run).
# uv runs from a build-time mount and keeps its download cache in a cache mount, so neither
# ships in an image.
FROM ghcr.io/astral-sh/uv:0.7.8@sha256:0178a92d156b6f6dbe60e3b52b33b421021f46d634aa9f81f42b91445bb81cdf AS uv

FROM python-system AS deps
COPY --link pyproject.toml uv.lock ./
# Keep locked third-party dependencies reusable when application code changes.
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --link-mode=copy

# The test-only dependency group, over the same lock: a superset of deps' environment.
FROM deps AS dev-deps
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --link-mode=copy

# Build the operator console (React/Vite) bundle so EVERY image build path --
# pipeline.yml's release images job, checks.yml, `docker compose up`, and a
# bare `docker build` -- ships central/console/dist/, independent of any
# workspace pre-build. This is the fix for the published central image 500ing on
# `GET /`: previously dist/ only reached the image when a caller happened to have
# pre-built it into the build context (checks.yml did; the publish path did not).
# Digest-pinned to match the python base's rigor (node:20-bookworm-slim,
# multi-arch index digest resolved 2026-09).
FROM node:20-bookworm-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS console-builder
WORKDIR /console
COPY central/console/package.json central/console/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY central/console/ ./
RUN npm run build

# The application source every service target ships, assembled once. The built console bundle
# always comes from the console-builder stage above, never from the build context
# (central/console/dist/ is .dockerignore'd), so no build path can smuggle in a stale dist or
# ship none at all. Linked copies keep a Python-only change from needing that stage's layers.
FROM scratch AS source
COPY --link contracts /app/contracts
COPY --link media /app/media
COPY --link player /app/player
COPY --link central /app/central
COPY --link nodeapi /app/nodeapi
COPY --link --from=console-builder /console/dist /app/central/console/dist

# GHA passes an immutable retained native image; Compose can use the local target.
FROM ${MEDIA_BASE_IMAGE} AS media-worker
# Both service targets share the same Python base and absolute environment path.
COPY --link --from=deps /app /app
COPY --link --from=source /app /app
# The project itself: an editable install, i.e. a .pth naming /app. Its build backend also
# leaves bytecode for the standard-library modules it imports, which the pyc-free Python base
# lacks and a read-only container cannot write; interpreter start-up depends on it, so keep it.
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --link-mode=copy
# gosu ships in the shared media OS base (installed before the definition
# boundary, alongside ffmpeg, per the repo's "all apt lives in the hashed base"
# invariant). docker-entrypoint.sh uses it to drop back to an unprivileged uid
# after it takes ownership of platform-supplied storage. The baked non-root
# `USER wall` default is retained so Compose runs exactly as before; a root-start
# platform (k8s securityContext) opts into the chown+gosu path.
COPY --link --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
# 0013: materialize the single cache root and its domain subdirs owned by the
# runtime identity BEFORE `VOLUME` (writes to a declared volume path are
# discarded), so a Docker volume is populated writable with no boot step. Runs
# as root -- this stage's base sets no USER; the `USER wall` default follows.
RUN install -d -o "$PHOTO_WALL_PUID" -g "$PHOTO_WALL_PGID" -m 0700 \
    "$PHOTO_WALL_CACHE_ROOT" \
    "$PHOTO_WALL_CACHE_ROOT/media" \
    "$PHOTO_WALL_CACHE_ROOT/apps" \
    "$PHOTO_WALL_CACHE_ROOT/os-images" \
    "$PHOTO_WALL_CACHE_ROOT/previews"
# The hub's configuration directory (PHOTO_WALL_HUB_CONFIG): the worker writes hub.json there and
# the hub reads it. Owned by the runtime identity so a fresh Docker volume mounted here is populated
# writable for `wall`; nothing in it outlives a hub start (E3b design §12).
RUN install -d -o "$PHOTO_WALL_PUID" -g "$PHOTO_WALL_PGID" -m 0755 /var/lib/photo-wall/hub
VOLUME ${PHOTO_WALL_CACHE_ROOT}
USER wall
ENV PHOTO_WALL_CONNECTIONS_FILE="/etc/photo-wall/private/connections.json"
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "media.worker"]

FROM media-worker AS media-test
# dev-deps' environment is a superset of the worker's third-party one from the same lock, so
# layering it over the worker adds the test group and leaves the project install in place.
COPY --link --from=dev-deps /app/.venv /app/.venv
COPY --link tests ./tests
CMD ["python", "-m", "pytest", "-q", "-o", "cache_dir=/tmp/pytest-cache", "tests/test_prepare.py"]

FROM deps AS central
COPY --link --from=source /app /app
# The project itself: an editable install, i.e. a .pth naming /app. Its build backend also
# leaves bytecode for the standard-library modules it imports, which the pyc-free Python base
# lacks and a read-only container cannot write; interpreter start-up depends on it, so keep it.
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --link-mode=copy
# 0013: central mounts the cache RO and creates NOTHING at runtime (the worker
# is the single writer -- see docs/decisions/0013-unified-cache-root.md "How
# ownership and writability work"). It still bakes the dir + `VOLUME` so the
# image path exists; `install -d` precedes `VOLUME` and runs as root (deps sets
# no USER), then drops to `wall`. No dir-creating entrypoint here.
RUN install -d -o "$PHOTO_WALL_PUID" -g "$PHOTO_WALL_PGID" -m 0700 \
    "$PHOTO_WALL_CACHE_ROOT" \
    "$PHOTO_WALL_CACHE_ROOT/media" \
    "$PHOTO_WALL_CACHE_ROOT/apps" \
    "$PHOTO_WALL_CACHE_ROOT/os-images" \
    "$PHOTO_WALL_CACHE_ROOT/previews"
VOLUME ${PHOTO_WALL_CACHE_ROOT}
USER wall
EXPOSE 8000
# --ws-max-size bounds one WebSocket frame. A Node's leaf rides Central's origin (/bus/leafnode), and
# nats-server writes everything it has pending for a connection as one frame, so it is at least
# node-bus.conf's max_pending plus one message (tests/test_node_bus_config.py; E-E3D-CC1-2).
CMD ["uvicorn", "central.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--ws-max-size", "16777216"]
