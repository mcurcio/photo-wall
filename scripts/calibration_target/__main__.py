"""`python -m scripts.calibration_target PHOTO --central URL`: read one photo of the
calibration targets and print one JSON object (decision 0027): {"reading": ...} or {"refused": ...},
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
import psycopg

from contracts.tuning import TestPattern

from .central_reads import CentralReads, HttpCentralReads
from .locate import locate
from .model import Refusal, Refused, to_json
from .photos import load_photo
from .read import read_photo
from .suggest import lut_acts, software_of, suggest

READ: int = 0
REFUSED: int = 2
ERROR: int = 1


def run(photo: str, central: CentralReads) -> tuple[dict[str, object], int]:
    """The command's JSON and exit status for the photo at `photo` against `central`: load_photo,
    locate, the entry its code names, read_photo, then suggest with the entry's LUT table when it
    names one."""
    located = locate(load_photo(Path(photo)))
    if isinstance(located, Refused):
        return _refused(located, Path(photo).name), REFUSED
    code = located.code
    entry = central.sent_entry(code)
    if entry is None:
        return _refused(Refused(Refusal.UNKNOWN_ENTRY, (),
                                f"Central has no picture change {code.picture_change} sent to "
                                f"{code.player_id} {code.output_id}"), None), REFUSED
    reading = read_photo(located, entry)
    if isinstance(reading, Refused):
        return _refused(reading, None), REFUSED
    lut = None
    if reading.stage is TestPattern.TARGET_POST_SOFTWARE:
        named = lut_acts(software_of(entry))
        lut = None if named is None else central.lut_table(named)
    return {"reading": to_json(reading), "suggestion": to_json(suggest(reading, lut))}, READ


def _refused(refused: Refused, photo: str | None) -> dict[str, object]:
    out = to_json(refused)
    if photo is not None:
        out["photo"] = photo
    return {"refused": out}


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the arguments, build HttpCentralReads from them and the environment, print `run`'s
    JSON (indented, sorted keys) and return its status."""
    parser = argparse.ArgumentParser(prog="python -m scripts.calibration_target",
                                     description="Read a photo of the calibration targets.")
    parser.add_argument("photo", metavar="PHOTO", help="one photo of both targets (ProRAW DNG or normal)")
    parser.add_argument("--central", required=True, help="Central's base URL")
    arguments = parser.parse_args(argv)
    token = os.environ.get("PHOTO_WALL_ADMIN_TOKEN")
    if not token:
        print("PHOTO_WALL_ADMIN_TOKEN is not set (run under `op run`)", file=sys.stderr)
        return ERROR
    central = HttpCentralReads(arguments.central, token, os.environ.get("PHOTO_WALL_READ_DATABASE_URL") or None)
    try:
        out, status = run(arguments.photo, central)
    except (ValueError, RuntimeError, OSError, httpx.HTTPError, psycopg.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return ERROR
    print(json.dumps(out, indent=2, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
