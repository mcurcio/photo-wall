FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.7.8@sha256:0178a92d156b6f6dbe60e3b52b33b421021f46d634aa9f81f42b91445bb81cdf /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY central ./central
COPY contracts ./contracts
COPY media ./media
COPY player ./player
RUN uv sync --frozen --no-dev && useradd --system --uid 10001 --create-home wall
USER wall
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "central.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
