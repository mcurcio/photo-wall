"""What the pinned downloads share: scripts/nats_server.py (the bus tests' nats-server) and
scripts/subject_model.py (the media worker's subject model).

A pin file's NAME=value lines (`read_pin`), the write beside the target and rename that keeps a
concurrent or interrupted fetch from leaving a partial file a later run would accept
(`write_beside`), and the `fetch --dest DIR` command line that prints the fetched path (`main`).
Stdlib only, and runnable by the system python3 before any venv (CI and the worker image's
`subject-model` stage call those scripts that way).
"""
from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO


def read_pin(text: str) -> dict[str, str]:
    """The NAME=value lines of a pin file (`#` comments and blank lines aside)."""
    return dict(line.split("=", 1) for line in map(str.strip, text.splitlines())
                if line and not line.startswith("#"))


def write_beside(target: Path, source: bytes | BinaryIO, mode: int) -> Path:
    """`source` written to `target` with `mode`: into a file beside it, then renamed over it, so
    a concurrent or interrupted fetch never leaves a partial file where the next run's cache
    check would accept it. Returns `target`."""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, partial = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}-")
    with os.fdopen(handle, "wb") as out:
        if isinstance(source, bytes):
            out.write(source)
        else:
            shutil.copyfileobj(source, out)
    os.chmod(partial, mode)
    os.replace(partial, target)
    return target


def main(description: str, what: str, fetch: Callable[[Path], Path]) -> None:
    """The `fetch --dest DIR` command line: runs `fetch(DIR)` and prints the path it returns;
    `what` names the download in the help."""
    parser = argparse.ArgumentParser(description=description)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch_command = commands.add_parser("fetch", help=f"download the pinned {what}; print its path")
    fetch_command.add_argument("--dest", type=Path, required=True)
    print(fetch(parser.parse_args().dest))
