"""Run pytest with the database tests pointed at the disposable test database.

Start that server first (tests/integration/compose.test-database.yml; the runbook's test
section). Each database test clones a migrated template into its own database and drops it
after; nothing touches a deployment database. An explicit PHOTO_WALL_TEST_DATABASE_URL (any
server where the user may CREATE DATABASE) takes precedence.
"""

import os
import subprocess
import sys
from pathlib import Path

# The fixed, test-only credential of the loopback-bound, tmpfs-backed test database.
TEST_DATABASE_URL = ("postgresql://photo_wall_test:isolated-test-only@127.0.0.1:{port}"
                     "/photo_wall_test")


def main():
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.setdefault("PHOTO_WALL_TEST_DATABASE_URL", TEST_DATABASE_URL.format(
        port=env.get("PHOTO_WALL_TEST_DB_PORT", "54330")))
    return subprocess.call([sys.executable, "-m", "pytest", *sys.argv[1:]], cwd=root, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
