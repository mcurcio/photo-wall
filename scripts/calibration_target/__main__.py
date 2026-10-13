"""`python -m scripts.calibration_target PHOTO [PHOTO] --central URL`: read one or two photos of the
calibration targets and print one JSON object (decision 0026): {"reading": ...} or {"refused": ...},
and with a reading {"suggestion": ...}. The admin token comes from PHOTO_WALL_ADMIN_TOKEN, the
optional read-only database address from PHOTO_WALL_READ_DATABASE_URL (run under `op run`).
Exit status 0 for a reading, 2 for a refusal, 1 for an error (a file unreadable, Central
unreachable)."""
from __future__ import annotations

from collections.abc import Sequence

from .central_reads import CentralReads


def run(photos: Sequence[str], central: CentralReads) -> tuple[dict[str, object], int]:
    """The command's JSON and exit status for `photos` (one or two paths) against `central`:
    load_photo, locate each, the entry for the first code, read_photos, then suggest with the
    entry's LUT table when it names one. ValueError("photos") for none or more than two paths."""
    ...


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the arguments, build HttpCentralReads from them and the environment, print `run`'s
    JSON (indented, sorted keys) and return its status."""
    ...


if __name__ == "__main__":
    raise SystemExit(main())
