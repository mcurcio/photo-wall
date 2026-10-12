"""A synthetic paper profile for the LUT recipe's tests: an ICC v2 output (printer) profile of RGB
device values with lut16 tables, written here byte by byte (never a real paper maker's file).

Pillow's ImageCms can only create matrix-shaper profiles (sRGB, Lab, XYZ), whose paper black is 0
and which carry no perceptual table, so neither the recipe's rendering intents nor its black point
compensation can change what they bake. This paper has both:

- **A raised paper black.** Its colorimetric tables (A2B1, B2A1) print device values d (encoded
  with DEVICE_GAMMA, as a printer's RGB is) through the sRGB primaries (D50) onto paper whose
  darkest ink still reflects `PAPER_BLACK` of its white: XYZ = PAPER_BLACK * W + (1 - PAPER_BLACK)
  * M d ** DEVICE_GAMMA, paper white W = D50 (so media-relative and absolute colorimetric agree).
- **A perceptual table that differs.** Its perceptual tables (A2B0, B2A0) are the colorimetric
  ones with the PCS chroma (a*, b*) of a device colour PERCEPTUAL_CHROMA times larger, so printing
  perceptually (B2A0) compresses chroma by 1 / PERCEPTUAL_CHROMA, as a paper's gamut mapping does,
  and a swapped intent moves every coloured point.

PCS is CIELAB in the ICC v2 lut16 encoding: L* 0..100 as 0..0xFF00, a* and b* -128..127.996 as
(value + 128) * 256.
"""
from __future__ import annotations

import itertools
import struct

PAPER_BLACK = 0.04          # the darkest ink's reflectance, a share of the paper's white (L* ~ 23.7)
PERCEPTUAL_CHROMA = 1.25    # the perceptual tables' chroma, against the colorimetric ones
DEVICE_GAMMA = 2.2
D50 = (0.9642, 1.0, 0.8249)
_SRGB_D50 = ((0.4360747, 0.3850649, 0.1430804),
             (0.2225045, 0.7168786, 0.0606169),
             (0.0139322, 0.0971045, 0.7141733))


def _inverse(m):
    (a, b, c), (d, e, f), (g, h, i) = m
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    return ((( e * i - f * h) / det, -(b * i - c * h) / det, (b * f - c * e) / det),
            (-(d * i - f * g) / det, (a * i - c * g) / det, -(a * f - c * d) / det),
            (( d * h - e * g) / det, -(a * h - b * g) / det, (a * e - b * d) / det))


_TO_DEVICE = _inverse(_SRGB_D50)


def _lab(xyz):
    def f(t):
        return t ** (1 / 3) if t > (6 / 29) ** 3 else t / (3 * (6 / 29) ** 2) + 4 / 29
    fx, fy, fz = (f(value / white) for value, white in zip(xyz, D50, strict=True))
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def _xyz(lab):
    lightness, a, b = lab
    fy = (lightness + 16) / 116
    fx, fz = fy + a / 500, fy - b / 200

    def g(t):
        return t ** 3 if t > 6 / 29 else 3 * (6 / 29) ** 2 * (t - 4 / 29)
    return tuple(white * g(t) for white, t in zip(D50, (fx, fy, fz), strict=True))


def device_to_lab(linear, chroma=1.0):
    """The paper's print of linear device values `linear` (each 0..1, d ** DEVICE_GAMMA) as CIELAB
    (D50), its chroma times `chroma`."""
    printed = tuple(PAPER_BLACK * white + (1 - PAPER_BLACK) * sum(m * v for m, v in zip(row, linear, strict=True))
                    for white, row in zip(D50, _SRGB_D50, strict=True))
    lightness, a, b = _lab(printed)
    return lightness, a * chroma, b * chroma


def lab_to_device(lab, chroma=1.0):
    """The linear device values that print `lab` (D50; its chroma divided by `chroma`), held to
    0..1 as ink is."""
    lightness, a, b = lab
    xyz = _xyz((lightness, a / chroma, b / chroma))
    paper = tuple((value - PAPER_BLACK * white) / (1 - PAPER_BLACK) for value, white in zip(xyz, D50, strict=True))
    return tuple(min(1.0, max(0.0, sum(m * v for m, v in zip(row, paper, strict=True)))) for row in _TO_DEVICE)


def _above_black(lightness: float) -> float:
    """How far L*'s light is above the paper's black, as a share of the rest: 0 at or below it."""
    luminance = _xyz((lightness, 0.0, 0.0))[1]
    return max(0.0, (luminance - PAPER_BLACK) / (1 - PAPER_BLACK))


def _lightness(above: float) -> float:
    return _lab((0.0, PAPER_BLACK + (1 - PAPER_BLACK) * above, 0.0))[0]


def _encode_lab(lab):
    lightness, a, b = lab
    return (round(min(max(lightness, 0.0), 100.0) * 0xFF00 / 100),
            round(min(max(a + 128, 0.0), 0xFFFF / 256) * 256),
            round(min(max(b + 128, 0.0), 0xFFFF / 256) * 256))


CURVE = 4096                # entries in each lut16 curve


def _curve(function) -> list[int]:
    """A lut16 curve of CURVE entries: function(x) for x in 0..1, both as 0..65535."""
    return [round(min(1.0, max(0.0, function(index / (CURVE - 1)))) * 0xFFFF) for index in range(CURVE)]


def _lut16(grid: int, inputs, point, outputs) -> bytes:
    """An mft2 (lut16Type) of three channels in and out: three input curves, a grid**3 table whose
    entry at (i, j, k) (first channel slowest) is point(i / (grid-1), j / ..., k / ...) (0..65535
    each), and three output curves."""
    matrix = struct.pack(">9i", 65536, 0, 0, 0, 65536, 0, 0, 0, 65536)
    table = b"".join(struct.pack(">3H", *point(*(index / (grid - 1) for index in indices)))
                     for indices in itertools.product(range(grid), repeat=3))
    curves = [struct.pack(f">{len(curve)}H", *curve) for curve in (*inputs, *outputs)]
    return (b"mft2" + bytes(4) + struct.pack(">BBBB", 3, 3, grid, 0) + matrix + struct.pack(">2H", CURVE, CURVE)
            + b"".join(curves[:3]) + table + b"".join(curves[3:]))


_SAME = _curve(lambda x: x)


def _a2b(chroma: float) -> bytes:
    """Device -> PCS: the input curves undo the device encoding, so the table is over linear device
    values (paper black at its corner)."""
    linear = _curve(lambda x: x ** DEVICE_GAMMA)
    return _lut16(17, (linear,) * 3, lambda *values: _encode_lab(device_to_lab(values, chroma)), (_SAME,) * 3)


def _b2a(chroma: float) -> bytes:
    """PCS -> device: L*'s input curve is the light above the paper's black (`_above_black`), so the
    paper's black is the table's first node and a neutral is linear between nodes; the output curves
    encode the device values."""
    above = _curve(lambda x: _above_black(min(x * 0xFFFF / 0xFF00, 1.0) * 100))

    def point(above_black, a, b):
        lab = (_lightness(above_black), a * 0xFFFF / 256 - 128, b * 0xFFFF / 256 - 128)
        return tuple(round(value * 0xFFFF) for value in lab_to_device(lab, chroma))
    encoded = _curve(lambda x: x ** (1 / DEVICE_GAMMA))
    return _lut16(33, (above, _SAME, _SAME), point, (encoded,) * 3)


def _xyz_tag(xyz) -> bytes:
    return b"XYZ " + bytes(4) + struct.pack(">3i", *(round(value * 65536) for value in xyz))


def _desc(text: str) -> bytes:
    ascii_text = text.encode("ascii") + b"\0"
    return (b"desc" + bytes(4) + struct.pack(">I", len(ascii_text)) + ascii_text
            + struct.pack(">II", 0, 0) + struct.pack(">HB", 0, 0) + bytes(67))


def lut_paper(name: str = "Synthetic matte paper") -> bytes:
    """The paper profile's bytes (see the module docstring)."""
    tags = [(b"desc", _desc(name)), (b"cprt", b"text" + bytes(4) + b"Public domain test fixture\0"),
            (b"wtpt", _xyz_tag(D50)),
            (b"A2B0", _a2b(PERCEPTUAL_CHROMA)), (b"B2A0", _b2a(PERCEPTUAL_CHROMA)),
            (b"A2B1", _a2b(1.0)), (b"B2A1", _b2a(1.0))]
    offset = 128 + 4 + 12 * len(tags)
    table, body = [], b""
    for signature, data in tags:
        padded = data + bytes(-len(data) % 4)
        table.append(struct.pack(">4sII", signature, offset + len(body), len(data)))
        body += padded
    size = offset + len(body)
    header = (struct.pack(">I", size) + bytes(4) + struct.pack(">I", 0x02100000) + b"prtr" + b"RGB " + b"Lab "
              + struct.pack(">6H", 2026, 10, 12, 0, 0, 0) + b"acsp" + bytes(4) + bytes(4) + bytes(4) + bytes(4)
              + bytes(8) + struct.pack(">I", 0) + struct.pack(">3i", *(round(value * 65536) for value in D50))
              + bytes(4) + bytes(16) + bytes(28))
    assert len(header) == 128
    return header + struct.pack(">I", len(tags)) + b"".join(table) + body
