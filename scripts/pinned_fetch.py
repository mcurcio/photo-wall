"""Pinned downloads: bytes the build takes from the network only when their sha256 is the one
recorded in the tree, cached by that digest.

Stdlib only, runnable by the system python3 (CI's `node-bus` job runs `scripts/nats_server.py`
before any venv). Consumers: `scripts/nats_server.fetch` (the base's nats-server) and, from E3c's
S4, the vendored wheels.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.request
from pathlib import Path


def fetch_pinned(url: str, sha256: str, *, timeout: float = 120.0) -> bytes:
    """The bytes at `url`; ValueError("pinned_fetch_digest") unless their sha256 is `sha256`."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError("pinned_fetch_digest")
    return data


def cached_pinned(url: str, sha256: str, cache: Path, *, timeout: float = 120.0) -> Path:
    """cache/<sha256>: fetched once with fetch_pinned, written beside the target and renamed into
    place, so a concurrent or interrupted fetch never leaves a partial file there. An existing file
    is re-hashed and refused with ValueError("pinned_fetch_digest") if it differs."""
    target = Path(cache) / sha256
    if target.is_file():
        with target.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != sha256:
                raise ValueError("pinned_fetch_digest")
        return target
    data = fetch_pinned(url, sha256, timeout=timeout)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, partial = tempfile.mkstemp(dir=target.parent, prefix=f".{sha256[:12]}-")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(partial, target)
    except BaseException:
        Path(partial).unlink(missing_ok=True)
        raise
    return target
