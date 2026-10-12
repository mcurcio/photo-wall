"""A colour temperature as linear-light white: the one kelvin-to-white conversion (print look,
design rule "one home"). Stdlib and contracts only: the Pi runs it.

Two consumers, one table:
- the Display's Photo Wall (software) colour temperature, KELVIN_MIN..KELVIN_MAX of
  contracts/node_picture.py, which the Pi's picture controller turns into the Player's white
  balance (appliance/display_host/picture.py `operations_for`);
- a Frame's mat light, LIGHT_KELVIN_MIN..LIGHT_KELVIN_MAX of contracts/models.py `Light`, which
  the Player turns into the light's colour against the Display's white (player/geometry.py, the
  mat shadow).

Moved here from appliance/display_host/picture.py (`kelvin_multipliers`, `_CHARITY`) when the
mat's lights needed it on the Player too, and widened from 3000..9300 to 2000..10000 for a warm
white bulb (2700 K) and a cool window. The rows 3000..9300 are the ones the picture split
shipped, unchanged (4900 K's blue stays 0.5796 where the source now reads 0.5797).
"""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from contracts.node_frame import srgb_to_linear

KELVIN_TABLE_MIN: Final = 2000                  # the table's rows, in KELVIN_TABLE_STEP
KELVIN_TABLE_MAX: Final = 10000
KELVIN_TABLE_STEP: Final = 100
KELVIN_WHITE: Final = 6500                      # the Display's white: kelvin_white(6500) is exactly (1, 1, 1)

# Mitchell Charity's blackbody colours (http://www.vendian.org/mncharity/dir3/blackbody/,
# bbr_color.txt version 2001-Jun-22): the CIE 1931 2-degree rows, R G B normalised, sRGB primaries
# with a D65 white point, KELVIN_TABLE_MIN..KELVIN_TABLE_MAX in KELVIN_TABLE_STEP.
_CHARITY: Final[Mapping[int, tuple[float, float, float]]] = MappingProxyType({
    2000: (1.0000, 0.2647, 0.0033), 2100: (1.0000, 0.2889, 0.0120), 2200: (1.0000, 0.3126, 0.0219),
    2300: (1.0000, 0.3360, 0.0331), 2400: (1.0000, 0.3589, 0.0454), 2500: (1.0000, 0.3814, 0.0588),
    2600: (1.0000, 0.4034, 0.0734), 2700: (1.0000, 0.4250, 0.0889), 2800: (1.0000, 0.4461, 0.1054),
    2900: (1.0000, 0.4668, 0.1229),
    3000: (1.0000, 0.4870, 0.1411), 3100: (1.0000, 0.5067, 0.1602), 3200: (1.0000, 0.5259, 0.1800),
    3300: (1.0000, 0.5447, 0.2005), 3400: (1.0000, 0.5630, 0.2216), 3500: (1.0000, 0.5809, 0.2433),
    3600: (1.0000, 0.5983, 0.2655), 3700: (1.0000, 0.6153, 0.2881), 3800: (1.0000, 0.6318, 0.3112),
    3900: (1.0000, 0.6480, 0.3346), 4000: (1.0000, 0.6636, 0.3583), 4100: (1.0000, 0.6789, 0.3823),
    4200: (1.0000, 0.6938, 0.4066), 4300: (1.0000, 0.7083, 0.4310), 4400: (1.0000, 0.7223, 0.4556),
    4500: (1.0000, 0.7360, 0.4803), 4600: (1.0000, 0.7494, 0.5051), 4700: (1.0000, 0.7623, 0.5299),
    4800: (1.0000, 0.7750, 0.5548), 4900: (1.0000, 0.7872, 0.5796), 5000: (1.0000, 0.7992, 0.6045),
    5100: (1.0000, 0.8108, 0.6293), 5200: (1.0000, 0.8221, 0.6541), 5300: (1.0000, 0.8330, 0.6787),
    5400: (1.0000, 0.8437, 0.7032), 5500: (1.0000, 0.8541, 0.7277), 5600: (1.0000, 0.8642, 0.7519),
    5700: (1.0000, 0.8740, 0.7760), 5800: (1.0000, 0.8836, 0.8000), 5900: (1.0000, 0.8929, 0.8238),
    6000: (1.0000, 0.9019, 0.8473), 6100: (1.0000, 0.9107, 0.8707), 6200: (1.0000, 0.9193, 0.8939),
    6300: (1.0000, 0.9276, 0.9168), 6400: (1.0000, 0.9357, 0.9396), 6500: (1.0000, 0.9436, 0.9621),
    6600: (1.0000, 0.9513, 0.9844), 6700: (0.9937, 0.9526, 1.0000), 6800: (0.9726, 0.9395, 1.0000),
    6900: (0.9526, 0.9270, 1.0000), 7000: (0.9337, 0.9150, 1.0000), 7100: (0.9157, 0.9035, 1.0000),
    7200: (0.8986, 0.8925, 1.0000), 7300: (0.8823, 0.8819, 1.0000), 7400: (0.8668, 0.8718, 1.0000),
    7500: (0.8520, 0.8621, 1.0000), 7600: (0.8379, 0.8527, 1.0000), 7700: (0.8244, 0.8437, 1.0000),
    7800: (0.8115, 0.8351, 1.0000), 7900: (0.7992, 0.8268, 1.0000), 8000: (0.7874, 0.8187, 1.0000),
    8100: (0.7761, 0.8110, 1.0000), 8200: (0.7652, 0.8035, 1.0000), 8300: (0.7548, 0.7963, 1.0000),
    8400: (0.7449, 0.7894, 1.0000), 8500: (0.7353, 0.7827, 1.0000), 8600: (0.7260, 0.7762, 1.0000),
    8700: (0.7172, 0.7699, 1.0000), 8800: (0.7086, 0.7638, 1.0000), 8900: (0.7004, 0.7579, 1.0000),
    9000: (0.6925, 0.7522, 1.0000), 9100: (0.6848, 0.7467, 1.0000), 9200: (0.6774, 0.7414, 1.0000),
    9300: (0.6703, 0.7362, 1.0000),
    9400: (0.6635, 0.7311, 1.0000), 9500: (0.6568, 0.7263, 1.0000), 9600: (0.6504, 0.7215, 1.0000),
    9700: (0.6442, 0.7169, 1.0000), 9800: (0.6382, 0.7124, 1.0000), 9900: (0.6324, 0.7081, 1.0000),
    10000: (0.6268, 0.7039, 1.0000),
})


def kelvin_white(kelvin: int) -> tuple[float, float, float]:
    """`kelvin`'s white in linear light, against the Display's white: Charity's row (gamma-encoded
    sRGB) taken to linear light (`srgb_to_linear`), divided by its linear KELVIN_WHITE row and
    scaled so the largest is 1. So kelvin_white(KELVIN_WHITE) is exactly (1.0, 1.0, 1.0), and white
    multiplied by it shows as the table's colour. Pure. ValueError("kelvin") for anything but an int
    (never a bool) in KELVIN_TABLE_MIN..KELVIN_TABLE_MAX in KELVIN_TABLE_STEP."""
    if type(kelvin) is not int or kelvin not in _CHARITY:
        raise ValueError("kelvin")
    red, green, blue = (srgb_to_linear(value) / srgb_to_linear(white)
                        for value, white in zip(_CHARITY[kelvin], _CHARITY[KELVIN_WHITE], strict=True))
    largest = max(red, green, blue)
    return red / largest, green / largest, blue / largest


__all__ = ["KELVIN_TABLE_MAX", "KELVIN_TABLE_MIN", "KELVIN_TABLE_STEP", "KELVIN_WHITE", "kelvin_white"]
