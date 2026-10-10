"""A display's EDID to its identity and modes (roadmap 1b; run ledger .claude/runs/display-1b.md,
slice D1). Pure: bytes in, values out; the read of /sys/class/drm/<card>-<output>/edid is the
caller's (`read_edid`).

The Pi normalises what the EDID says and never decides "same display": that is Central's
(central/displays/). Spike, 2026-10-10: the test monitor's EDID is maker XYM, product 5475, name
MNN, serial 0 and blank serial text, so `serial` is None for it.
"""
from __future__ import annotations

import glob
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from contracts.node_output import MAX_DISPLAY_MODES, DisplayIdentity, DisplayMode

DRM_ROOT: Final = Path("/sys/class/drm")
MAX_EDID_BYTES: Final = 32 * 1024     # a base block and up to 255 extensions

# The EDID layout (VESA E-EDID 1.4, CTA-861) this module reads; one home for every offset.
_BLOCK: Final = 128
_HEADER: Final = bytes((0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00))
_MAKER: Final = slice(8, 10)          # three 5-bit letters, big-endian, "A" = 1
_PRODUCT: Final = slice(10, 12)       # little-endian
_SERIAL_NUMBER: Final = slice(12, 16)  # little-endian
_DESCRIPTORS: Final = (54, 72, 90, 108)
_EXTENSIONS: Final = 126
_DESCRIPTOR: Final = 18
_NAME_TAG: Final = 0xFC
_SERIAL_TAG: Final = 0xFF
_CTA_TAG: Final = 0x02
_VIDEO_BLOCK: Final = 2               # CTA data block tag of the short video descriptors

# CTA-861 VICs as (width, height, refresh mHz), progressive and not pixel-repeated only: the
# compositor drives neither interlaced nor pixel-repeated modes. A VIC not listed is skipped.
_VICS: Final[dict[int, tuple[int, int, int]]] = {
    1: (640, 480, 60000), 2: (720, 480, 60000), 3: (720, 480, 60000), 4: (1280, 720, 60000),
    16: (1920, 1080, 60000), 17: (720, 576, 50000), 18: (720, 576, 50000), 19: (1280, 720, 50000),
    31: (1920, 1080, 50000), 32: (1920, 1080, 24000), 33: (1920, 1080, 25000), 34: (1920, 1080, 30000),
    41: (1280, 720, 100000), 42: (720, 576, 100000), 43: (720, 576, 100000), 47: (1280, 720, 120000),
    48: (720, 480, 120000), 49: (720, 480, 120000), 60: (1280, 720, 24000), 61: (1280, 720, 25000),
    62: (1280, 720, 30000), 63: (1920, 1080, 120000), 64: (1920, 1080, 100000),
    93: (3840, 2160, 24000), 94: (3840, 2160, 25000), 95: (3840, 2160, 30000), 96: (3840, 2160, 50000),
    97: (3840, 2160, 60000), 98: (4096, 2160, 24000), 99: (4096, 2160, 25000), 100: (4096, 2160, 30000),
    101: (4096, 2160, 50000), 102: (4096, 2160, 60000),
}


def parse_edid(raw: bytes) -> tuple[DisplayIdentity | None, tuple[DisplayMode, ...]]:
    """The identity and the modes `raw` states. Identity None when the base block is absent,
    shorter than 128 bytes, has a wrong header or checksum; modes come from the detailed timing
    descriptors and the CTA extension's short video descriptors that the base block's checksum
    covers, deduplicated, the preferred one first, at most MAX_DISPLAY_MODES. Never raises."""
    base = raw[:_BLOCK] if isinstance(raw, bytes) else b""
    if not _valid_block(base) or base[:len(_HEADER)] != _HEADER:
        return None, ()
    return _identity(base), _modes(raw, base)


def _valid_block(block: bytes) -> bool:
    return len(block) == _BLOCK and sum(block) % 256 == 0


def _identity(base: bytes) -> DisplayIdentity | None:
    """The base block's identity; None when what it states is not a valid DisplayIdentity."""
    packed = int.from_bytes(base[_MAKER], "big")
    letters = [(packed >> shift) & 0x1F for shift in (10, 5, 0)]
    texts = {tag: text for tag, text in _texts(base)}
    number = int.from_bytes(base[_SERIAL_NUMBER], "little")
    serial = texts.get(_SERIAL_TAG) or (str(number) if number else None)
    try:
        return DisplayIdentity("".join(chr(ord("A") - 1 + letter) for letter in letters),
                               int.from_bytes(base[_PRODUCT], "little"), texts.get(_NAME_TAG, ""), serial)
    except ValueError:
        return None


def _texts(base: bytes) -> Iterator[tuple[int, str]]:
    """The base block's text descriptors as (tag, text): up to 13 characters ended by a line feed,
    padded with spaces; non-printable characters dropped."""
    for offset in _DESCRIPTORS:
        descriptor = base[offset:offset + _DESCRIPTOR]
        if descriptor[0:2] == b"\0\0" and descriptor[3] in (_NAME_TAG, _SERIAL_TAG):
            text = descriptor[5:].split(b"\n", 1)[0].decode("latin-1")
            yield descriptor[3], "".join(char for char in text if char.isprintable()).strip()


def _modes(raw: bytes, base: bytes) -> tuple[DisplayMode, ...]:
    """The base block's detailed timings (the first one is the preferred mode), then each valid CTA
    extension's short video descriptors and detailed timings; deduplicated (the first kept), capped
    at MAX_DISPLAY_MODES."""
    timings = [*_detailed(base, _DESCRIPTORS)]
    for index in range(base[_EXTENSIONS]):
        block = raw[_BLOCK * (index + 1):_BLOCK * (index + 2)]
        if _valid_block(block) and block[0] == _CTA_TAG:
            timings.extend(_short_video(block))
            if block[2] >= 4:
                timings.extend(_detailed(block, range(block[2], _BLOCK - _DESCRIPTOR, _DESCRIPTOR)))
    preferred = next(_detailed(base, _DESCRIPTORS[:1]), None)    # EDID 1.4: the first descriptor
    found: dict[tuple[int, int, int], DisplayMode] = {}
    for timing in timings:
        try:
            mode = DisplayMode(*timing, preferred=timing == preferred)
        except ValueError:
            continue
        found.setdefault(timing, mode)
        if len(found) == MAX_DISPLAY_MODES:
            break
    return tuple(found.values())


def _detailed(block: bytes, offsets: range | tuple[int, ...]) -> Iterator[tuple[int, int, int]]:
    """Each progressive detailed timing descriptor at `offsets` as (width, height, refresh mHz)."""
    for offset in offsets:
        d = block[offset:offset + _DESCRIPTOR]
        clock_hz = int.from_bytes(d[0:2], "little") * 10_000
        if len(d) < _DESCRIPTOR or clock_hz == 0 or d[17] & 0x80:   # a text descriptor, or interlaced
            continue
        width, blank_h = d[2] | (d[4] & 0xF0) << 4, d[3] | (d[4] & 0x0F) << 8
        height, blank_v = d[5] | (d[7] & 0xF0) << 4, d[6] | (d[7] & 0x0F) << 8
        total = (width + blank_h) * (height + blank_v)
        if total:
            yield width, height, round(clock_hz * 1000 / total)


def _short_video(block: bytes) -> Iterator[tuple[int, int, int]]:
    """The listed VICs of a CTA extension's video data blocks (bytes 4 up to its timing offset)."""
    offset, end = 4, min(block[2], _BLOCK - 1)
    while offset < end:
        tag, length = block[offset] >> 5, block[offset] & 0x1F
        if tag == _VIDEO_BLOCK:
            for svd in block[offset + 1:min(offset + 1 + length, end)]:
                vic = svd & 0x7F if 129 <= svd <= 192 else svd    # 129..192: VIC 1..64 marked native
                if vic in _VICS:
                    yield _VICS[vic]
        offset += 1 + length


def read_edid(output_id: str, *, root: Path = DRM_ROOT) -> bytes | None:
    """The EDID bytes of the connector named `output_id` (`<root>/card*-<output_id>/edid`, at most
    MAX_EDID_BYTES), or None when no such connector exists or it holds no EDID. Never raises."""
    try:
        connectors = sorted(root.glob(f"card*-{glob.escape(output_id)}"))
    except (OSError, TypeError, ValueError):
        return None
    for connector in connectors:
        try:
            with open(connector / "edid", "rb") as source:
                raw = source.read(MAX_EDID_BYTES)
        except OSError:
            continue
        if raw:
            return raw
    return None
