"""The pinned subject model (media/subject_map.py): the U-2-Netp file the media worker loads.

The pin is media/subject_model.env, its one home (URL, SHA-256, size). `python3
scripts/subject_model.py fetch --dest DIR` downloads it once into DIR/<sha256>/u2netp.onnx,
refuses it unless its size and SHA-256 are the pin's, and prints its path; a second run with
the file in place verifies it and downloads nothing. The worker image's `subject-model` stage
(Dockerfile) and every CI leg that runs the measurement (PHOTO_WALL_SUBJECT_MODEL) call it.
Stdlib only, and runnable by the system python3 before any venv; the pin read, the atomic write
and the command line are scripts/pinned_fetch.py's, shared with scripts/nats_server.py.
"""
from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.pinned_fetch import main, read_pin, write_beside  # noqa: E402

PIN_FILE: Final = ROOT / "media/subject_model.env"
FILE_NAME: Final = "u2netp.onnx"
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
    """The pinned model under `dest` (module docstring), written atomically (`write_beside`)."""
    model = Path(dest) / SHA256 / FILE_NAME
    if model.is_file():
        verified(model.read_bytes())
        return model
    with urllib.request.urlopen(URL, timeout=120) as response:
        data = verified(response.read(BYTES + 1))
    return write_beside(model, data, 0o644)


if __name__ == "__main__":
    main(__doc__.splitlines()[0], "model", fetch)
