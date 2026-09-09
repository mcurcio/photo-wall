"""Emit only neutral contracts, Player and native fixtures to a Docker context."""
from __future__ import annotations

import sys
import tarfile
from pathlib import Path

root = Path(__file__).resolve().parents[2]
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|", format=tarfile.GNU_FORMAT) as archive:
    archive.add(root/"tests/native/Dockerfile", arcname="Dockerfile", recursive=False)
    for directory in ("contracts", "player", "tests/native"):
        for path in sorted((root/directory).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and not path.is_symlink():
                archive.add(path, arcname=str(path.relative_to(root)), recursive=False)
