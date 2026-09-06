FROM python:3.12.11-slim-trixie@sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f AS runtime
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.7.8@sha256:0178a92d156b6f6dbe60e3b52b33b421021f46d634aa9f81f42b91445bb81cdf /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
# Keep locked third-party dependencies reusable when application code changes.
RUN uv sync --frozen --no-dev --no-install-project && useradd --system --uid 10001 --create-home wall \
    && install -d -o wall -g wall -m 0700 /var/lib/photo-wall/media /etc/photo-wall/private
COPY central ./central
COPY contracts ./contracts
COPY media ./media
COPY player ./player
RUN uv sync --frozen --no-dev
USER wall
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

FROM runtime AS media-worker
USER root
RUN rm -f /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' \
       'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20260905T000000Z trixie main' \
       'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/20260905T000000Z trixie-security main' \
       > /etc/apt/sources.list \
    && apt-get -o Acquire::Retries=3 update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n' > /etc/photo-wall/packages.tsv \
    && sha256sum /usr/bin/ffmpeg /usr/bin/ffprobe > /etc/photo-wall/conversion-binaries.sha256 \
    && printf '%s\n' '{"schema":1,"connections":[]}' > /etc/photo-wall/private/connections.json \
    && chown wall:wall /etc/photo-wall/private/connections.json \
    && chmod 0600 /etc/photo-wall/private/connections.json \
    && rm -rf /var/lib/apt/lists/*
USER wall
ENV PHOTO_WALL_MEDIA_ROOT="/var/lib/photo-wall/media" \
    PHOTO_WALL_CONNECTIONS_FILE="/etc/photo-wall/private/connections.json"
CMD ["python", "-m", "media.worker"]

FROM media-worker AS media-test
USER root
RUN uv sync --frozen
COPY tests ./tests
USER wall
CMD ["python", "-m", "pytest", "-q", "-o", "cache_dir=/tmp/pytest-cache", "tests/test_prepare.py"]

FROM runtime AS central
EXPOSE 8000
CMD ["uvicorn", "central.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--ws-max-size", "1048576"]
