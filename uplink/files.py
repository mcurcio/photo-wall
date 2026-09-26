"""The one way a root stage leaves a file for a later stage: written whole or not at all."""

import os
import tempfile
from pathlib import Path


def write_atomically(path: Path, data: bytes, *, mode: int) -> None:
    """Write `data` to a temporary file beside `path`, fsync it, set `mode` explicitly (later
    stages run under UMask=0077, so the umask must not decide it), then replace `path`. A
    reader sees the old file or the new one, never a part. Parents are created 0755."""
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
