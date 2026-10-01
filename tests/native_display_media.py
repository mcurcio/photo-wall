"""Deterministic private-media-free image for the actual renderer fixture."""
import hashlib
import struct
import zlib

from contracts.models import Variant


def image_fixture():
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
    size = 64
    rows = b"".join(b"\0" + b"".join(
        bytes((255, 80, 30) if (x // 8 + y // 8) % 2 else (30, 120, 255))
        for x in range(size)) for y in range(size))
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!2I5B", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    return data, Variant(sha256=hashlib.sha256(data).hexdigest(), size=len(data),
                         media_type="image/png", width=size, height=size)
