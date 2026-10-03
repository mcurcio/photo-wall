"""The media worker's container healthcheck: `python -m media.healthcheck` (compose.yaml).

Healthy when the worker checked in within `WORKER_FRESH_SECONDS` (the console's worker-quiet
window, matched with the console's `<=`), aged on the database's clock (G11) -- never this process's clock, which may disagree with the database's.
"""

from __future__ import annotations

import os

from media.task_queue import WORKER_FRESH_SECONDS
from media.worker import build_repository


def main() -> int:
    dsn = os.environ.get("PHOTO_WALL_DATABASE_URL")
    if not dsn:
        return 1
    repository = build_repository(dsn)
    try:
        age = repository.worker_age()
    except Exception:
        return 1
    finally:
        repository.db.close()
    return 0 if age is not None and age <= WORKER_FRESH_SECONDS else 1


if __name__ == "__main__":
    raise SystemExit(main())
