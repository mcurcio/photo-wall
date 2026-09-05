"""Run portable and real PostgreSQL checks against an already-running local Compose DB.

Every DB test creates and removes its own random schema; deployment data is preserved.
The private local environment is parsed as data, never sourced as shell code.
"""

import os
import subprocess
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    values = dict(line.split("=", 1) for line in (root / ".env").read_text().splitlines()
                  if line and not line.startswith("#") and "=" in line)
    password = values["PHOTO_WALL_DB_PASSWORD"]
    port = values.get("PHOTO_WALL_DB_PORT", "54329")
    env = os.environ.copy()
    env["PHOTO_WALL_TEST_DATABASE_URL"] = f"postgresql://photo_wall:{password}@127.0.0.1:{port}/photo_wall"
    return subprocess.call([sys.executable, "-m", "pytest", *sys.argv[1:]], cwd=root, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
