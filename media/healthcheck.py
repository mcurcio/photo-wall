"""The media worker's container healthcheck: `python -m media.healthcheck` (compose.yaml).

Healthy when a worker beat its queue heartbeat within the stalled-worker window, by the
database's clock (`MediaRepository.worker_running`, central/infra/runtime.py
`worker_running_in`) -- never this process's clock, which may disagree with the database's.
"""

from __future__ import annotations

import os

from media.worker import build_repository


def main() -> int:
    dsn = os.environ.get("PHOTO_WALL_DATABASE_URL")
    if not dsn:
        return 1
    repository = build_repository(dsn)
    try:
        running = repository.worker_running()
    except Exception:
        return 1
    finally:
        repository.db.close()
    return 0 if running else 1


if __name__ == "__main__":
    raise SystemExit(main())
