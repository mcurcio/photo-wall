"""The repository root, for tests at any depth under `tests/`."""
from __future__ import annotations

from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parents[2]
