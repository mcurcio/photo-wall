"""Shared bounded, sanitized diagnostics for failed Docker commands."""

from __future__ import annotations

import os
import re
from pathlib import Path

MAX_DOCKER_DEBUG_LOG = 512 * 1024
MAX_DOCKER_DEBUG_ENTRY = 64 * 1024

_SECRET = re.compile(
    rb"(?i)((?:password|token|secret|authorization|api[_-]?key)[\"']?\s*[:=]\s*[\"']?)"
    rb"[^\s,\"']+"
)
_BEARER = re.compile(rb"(?i)bearer\s+[A-Za-z0-9._~+\-/=]{8,}")


def docker_debug_args(args: list[str]) -> list[str]:
    if os.environ.get("PHOTO_WALL_DOCKER_DEBUG") == "1" and args and args[0] == "docker":
        return ["docker", "--debug", *args[1:]]
    return args


def sanitize_docker_output(data: bytes) -> bytes:
    data = data.replace(b"\x00", b"?")
    data = _SECRET.sub(rb"\1<redacted>", data)
    return _BEARER.sub(b"Bearer <redacted>", data)


def record_docker_debug(args: list[str], code: int, data: bytes) -> None:
    """Append output without command arguments, credentials, or unbounded data."""
    value = os.environ.get("PHOTO_WALL_DOCKER_DEBUG_LOG")
    if not value or not args or args[0] != "docker":
        return
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        return
    operation = args[1] if len(args) > 1 and re.fullmatch(r"[a-z-]{1,32}", args[1]) else "unknown"
    diagnostic = sanitize_docker_output(data[-MAX_DOCKER_DEBUG_ENTRY:])
    payload = f"docker operation={operation} exit={code}\n".encode() + diagnostic + b"\n"
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        current = path.stat().st_size if path.exists() else 0
        if current >= MAX_DOCKER_DEBUG_LOG:
            return
        payload = payload[:MAX_DOCKER_DEBUG_LOG - current]
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
    except OSError:
        pass
