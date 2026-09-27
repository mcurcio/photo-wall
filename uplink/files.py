"""The one way a root stage leaves a file for a later stage: written whole or not at all."""

import os
import tempfile
from pathlib import Path


def _make_parents(directory: Path) -> None:
    """Create the missing directories down to `directory`, from the top, and chmod each one
    this call created to 0755: mkdir's mode is masked by the umask. An existing directory is
    left alone (never widen /run or /etc), including one another process creates meanwhile."""
    missing: list[Path] = []
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    for created in reversed(missing):
        try:
            created.mkdir(mode=0o755)
        except FileExistsError:
            continue
        os.chmod(created, 0o755)


def write_atomically(path: Path, data: bytes, *, mode: int) -> None:
    """Write `data` to a temporary file beside `path`, fsync it, set `mode` explicitly (later
    stages run under UMask=0077, so the umask must not decide it), then replace `path`. A
    reader sees the old file or the new one, never a part. The parents it creates are 0755,
    whatever the umask."""
    _make_parents(path.parent)
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
