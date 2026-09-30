"""Base-owned Player entrypoint; app payload data cannot choose a command or service unit."""

from __future__ import annotations

import os
import re
from pathlib import Path

from contracts.strict_json import loads_object

ROOTS = Path("/run/photo-wall/apps")
PYTHON = "/usr/bin/python3"
CONFIG = "/etc/photo-wall/public.json"
_SHA256 = re.compile(r"[0-9a-f]{64}")


def selected_app(roots: Path = ROOTS) -> Path:
    try:
        value = loads_object((roots / "active.json").read_bytes(), max_bytes=256)
    except OSError as exc:
        raise ValueError("active_root_unavailable") from exc
    if (value is None or set(value) != {"schema", "sha256"} or value["schema"] != 1
            or not isinstance(value["sha256"], str)
            or _SHA256.fullmatch(value["sha256"]) is None):
        raise ValueError("active_root_invalid")
    root = roots / value["sha256"]
    if root.is_symlink() or not (root / "app/__main__.py").is_file():
        raise ValueError("active_root_missing")
    return root / "app"


def main() -> None:
    app = selected_app()
    os.execv(PYTHON, [PYTHON, "-I", "-B", str(app), "--config", CONFIG])


if __name__ == "__main__":
    main()
