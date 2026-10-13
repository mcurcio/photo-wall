"""`python -m scripts.calibration_target PHOTO [PHOTO] --central URL`: read one or two photos of the
calibration targets and print one JSON object (decision 0026): {"reading": ...} or {"refused": ...},
and with a reading {"suggestion": ...}. The admin token comes from PHOTO_WALL_ADMIN_TOKEN, the
optional read-only database address from PHOTO_WALL_READ_DATABASE_URL (run under `op run`).
Exit status 0 for a reading, 2 for a refusal, 1 for an error (a file unreadable, Central
unreachable)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import httpx

from .central_reads import CentralReads, HttpCentralReads
from .locate import Located, locate
from .model import Refusal, Refused, to_json
from .photos import load_photo
from .read import read_photos

READ: int = 0
REFUSED: int = 2
ERROR: int = 1


def run(photos: Sequence[str], central: CentralReads) -> tuple[dict[str, object], int]:
    """The command's JSON and exit status for `photos` (one or two paths) against `central`:
    load_photo, locate each, the entry for the first code, read_photos, then suggest with the
    entry's LUT table when it names one. ValueError("photos") for none or more than two paths."""
    if not 1 <= len(photos) <= 2:
        raise ValueError("photos")
    located: list[Located] = []
    for path in photos:
        found = locate(load_photo(Path(path)))
        if isinstance(found, Refused):
            return _refused(found, Path(path).name), REFUSED
        located.append(found)
    entry = central.sent_entry(located[0].code)
    if entry is None:
        code = located[0].code
        return _refused(Refused(Refusal.UNKNOWN_ENTRY, (),
                                f"Central has no picture change {code.picture_change} sent to "
                                f"{code.player_id} {code.output_id}"), None), REFUSED
    reading = read_photos(tuple(located), entry)  # type: ignore[arg-type]
    if isinstance(reading, Refused):
        return _refused(reading, None), REFUSED
    return {"reading": to_json(reading)}, READ


def _refused(refused: Refused, photo: str | None) -> dict[str, object]:
    out = to_json(refused)
    if photo is not None:
        out["photo"] = photo
    return {"refused": out}


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the arguments, build HttpCentralReads from them and the environment, print `run`'s
    JSON (indented, sorted keys) and return its status."""
    parser = argparse.ArgumentParser(prog="python -m scripts.calibration_target",
                                     description="Read photos of the calibration targets.")
    parser.add_argument("photos", nargs="+", metavar="PHOTO", help="one or two photos (ProRAW DNG or normal)")
    parser.add_argument("--central", required=True, help="Central's base URL")
    arguments = parser.parse_args(argv)
    if len(arguments.photos) > 2:
        parser.error("one or two photos")
    token = os.environ.get("PHOTO_WALL_ADMIN_TOKEN")
    if not token:
        print("PHOTO_WALL_ADMIN_TOKEN is not set (run under `op run`)", file=sys.stderr)
        return ERROR
    central = HttpCentralReads(arguments.central, token, os.environ.get("PHOTO_WALL_READ_DATABASE_URL") or None)
    try:
        out, status = run(arguments.photos, central)
    except (ValueError, RuntimeError, OSError, httpx.HTTPError) as error:
        print(f"error: {error}", file=sys.stderr)
        return ERROR
    print(json.dumps(out, indent=2, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
