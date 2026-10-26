"""The pinned subject model (media/subject_map.py): the U-2-Netp file the media worker loads.

The pin is media/subject_model.env, its one home (URL, SHA-256, size). `python3
scripts/subject_model.py fetch --dest DIR` downloads it once into DIR/<sha256>/u2netp.onnx,
refuses it unless its size and SHA-256 are the pin's, and prints its path; a second run with
the file in place verifies it and downloads nothing. The worker image's `subject-model` stage
(Dockerfile) and every CI leg that runs the measurement (PHOTO_WALL_SUBJECT_MODEL) call it.
Stdlib only, and runnable by the system python3 before any venv (the scripts/nats_server.py
pattern; it reads its own pin, as importing that module reads the nats-server pin).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
import urllib.request
from pathlib import Path
from typing import Final

PIN_FILE: Final = Path(__file__).resolve().parents[1] / "media/subject_model.env"
FILE_NAME: Final = "u2netp.onnx"


def read_pin(text: str) -> dict[str, str]:
    """The NAME=value lines of a pin file (`#` comments and blank lines aside)."""
    return dict(line.split("=", 1) for line in map(str.strip, text.splitlines())
                if line and not line.startswith("#"))


PIN: Final = read_pin(PIN_FILE.read_text())
URL: Final = PIN["SUBJECT_MODEL_URL"]
SHA256: Final = PIN["SUBJECT_MODEL_SHA256"]
BYTES: Final = int(PIN["SUBJECT_MODEL_BYTES"])


def verified(data: bytes) -> bytes:
    """`data` when it is the pinned file; ValueError("subject_model_checksum") otherwise."""
    if len(data) != BYTES or hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError("subject_model_checksum")
    return data


def fetch(dest: Path) -> Path:
    """The pinned model under `dest` (module docstring), written atomically: a concurrent or
    interrupted fetch never leaves a partial file where the next run would accept it."""
    directory = Path(dest) / SHA256
    model = directory / FILE_NAME
    if model.is_file():
        verified(model.read_bytes())
        return model
    with urllib.request.urlopen(URL, timeout=120) as response:
        data = verified(response.read(BYTES + 1))
    directory.mkdir(parents=True, exist_ok=True)
    handle, partial = tempfile.mkstemp(dir=directory, prefix=".u2netp-")
    with os.fdopen(handle, "wb") as out:
        out.write(data)
    os.chmod(partial, 0o644)
    os.replace(partial, model)
    return model


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    fetch_command = commands.add_parser("fetch", help="download the pinned model; print its path")
    fetch_command.add_argument("--dest", type=Path, required=True)
    arguments = parser.parse_args()
    print(fetch(arguments.dest))


if __name__ == "__main__":
    main()
