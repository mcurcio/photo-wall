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

FROM python-system AS deps
COPY --from=ghcr.io/astral-sh/uv:0.7.8@sha256:0178a92d156b6f6dbe60e3b52b33b421021f46d634aa9f81f42b91445bb81cdf /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
# Keep locked third-party dependencies reusable when application code changes.
RUN uv sync --frozen --no-dev --no-install-project

# Build the operator console (React/Vite) bundle so EVERY image build path --
# release.yml's service-images publish, checks.yml, `docker compose up`, and a
# bare `docker build` -- ships central/console/dist/, independent of any
# workspace pre-build. This is the fix for the published central image 500ing on
# `GET /`: previously dist/ only reached the image when a caller happened to have
# pre-built it into the build context (checks.yml did; the publish path did not).
# Digest-pinned to match the python base's rigor (node:20-bookworm-slim,
# multi-arch index digest resolved 2026-09).
FROM node:20-bookworm-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS console-builder
WORKDIR /console
COPY central/console/package.json central/console/package-lock.json ./
RUN npm ci
COPY central/console/ ./
RUN npm run build

FROM deps AS runtime
COPY central ./central
# The built console bundle always comes from the console-builder stage above,
# never from the build context (central/console/dist/ is .dockerignore'd), so no
# build path can smuggle in a stale dist or ship none at all.
COPY --from=console-builder /console/dist ./central/console/dist
COPY contracts ./contracts
COPY media ./media
COPY player ./player
RUN uv sync --frozen --no-dev
USER wall

# GHA passes an immutable retained native image; Compose can use the local target.
FROM ${MEDIA_BASE_IMAGE} AS media-system
COPY --from=deps /usr/local/bin/uv /usr/local/bin/uv

FROM media-system AS media-worker
# Both branches share the same Python base and absolute environment path.
COPY --from=runtime /app /app
# gosu ships in the shared media OS base (installed before the definition
# boundary, alongside ffmpeg, per the repo's "all apt lives in the hashed base"
# invariant). docker-entrypoint.sh uses it to drop back to an unprivileged uid
# after it takes ownership of platform-supplied storage. The baked non-root
# `USER wall` default is retained so Compose runs exactly as before; a root-start
# platform (k8s securityContext) opts into the chown+gosu path.
COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
# 0013: materialize the single cache root and its domain subdirs owned by the
# runtime identity BEFORE `VOLUME` (writes to a declared volume path are
# discarded), so a Docker volume is populated writable with no boot step. Runs
# as root -- this stage's base sets no USER; the `USER wall` default follows.
RUN install -d -o "$PHOTO_WALL_PUID" -g "$PHOTO_WALL_PGID" -m 0700 \
    "$PHOTO_WALL_CACHE_ROOT" \
    "$PHOTO_WALL_CACHE_ROOT/media" \
    "$PHOTO_WALL_CACHE_ROOT/apps" \
    "$PHOTO_WALL_CACHE_ROOT/os-images"
VOLUME ${PHOTO_WALL_CACHE_ROOT}
USER wall
ENV PHOTO_WALL_CONNECTIONS_FILE="/etc/photo-wall/private/connections.json"
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "media.worker"]

FROM media-worker AS media-test
USER root
RUN uv sync --frozen
COPY tests ./tests
USER wall
CMD ["python", "-m", "pytest", "-q", "-o", "cache_dir=/tmp/pytest-cache", "tests/test_prepare.py"]

FROM runtime AS central
# 0013: central mounts the cache RO and creates NOTHING at runtime (the worker
# is the single writer -- see docs/decisions/0013-unified-cache-root.md "How
# ownership and writability work"). It still bakes the dir + `VOLUME` so the
# image path exists; `install -d` precedes `VOLUME` and runs as root (runtime
# left `USER wall`), then drops back to `wall`. No dir-creating entrypoint here.
USER root
RUN install -d -o "$PHOTO_WALL_PUID" -g "$PHOTO_WALL_PGID" -m 0700 \
    "$PHOTO_WALL_CACHE_ROOT" \
    "$PHOTO_WALL_CACHE_ROOT/media" \
    "$PHOTO_WALL_CACHE_ROOT/apps" \
    "$PHOTO_WALL_CACHE_ROOT/os-images"
VOLUME ${PHOTO_WALL_CACHE_ROOT}
USER wall
EXPOSE 8000
CMD ["uvicorn", "central.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--ws-max-size", "1048576"]
