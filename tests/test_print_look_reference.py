"""The print look's scalar reference (design /print-look, 2026-10-12, "The order" and "The mat
shadow"): saturation, the highlight rolloff, the black floor and the Frame's mat shadow in
player/geometry.py, each skipped at its neutral value. Pure: no GL (the shader is held to this
reference by tests/native_renderer_pixel_harness.py)."""
from __future__ import annotations

import itertools
import math
from dataclasses import replace

import pytest

from contracts.kelvin import kelvin_white
from contracts.lut import LUT_BYTES, LUT_GRID, LutRef, lut_offset
from contracts.models import CornersFit, CoverFit, FrameProfile, Light, Mat
from contracts.node_frame import LutMix, PictureOperations, srgb_to_linear
from player.geometry import (
    LUMA,
    decode_channel,
    encode_srgb,
    final_pixel,
    homography,
    light_colour,
    lut_applied,
    lut_sampled,
    mat_shade,
    mat_shadow,
    output_pixel,
    peak,
    rolled_off,
    rolloff_skipped,
    transform,
)

SCREEN = FrameProfile(width_px=1920, height_px=1080, diagonal_inches=27)     # 27" 16:9
WIDTH_MM = 27 * 25.4 * (16 / 9) / math.hypot(16 / 9, 1)
HEIGHT_MM = 27 * 25.4 / math.hypot(16 / 9, 1)


def operations(rgb=(1.0, 1.0, 1.0), light=1.0, slope=1.0, exponent=1.0, *, saturation=1.0,
               knee=1.0, floor=0.0) -> PictureOperations:
    return PictureOperations("HDMI-A-1", 1, rgb, light, slope, exponent, saturation=saturation,
                             highlight_knee=knee, black_floor=floor)


def light(from_degrees=0, steep=45, softness=0, tenths=10, kelvin=6500) -> Light:
    return Light(from_degrees=from_degrees, steep_degrees=steep, softness_degrees=softness,
                 strength_tenths=tenths, colour_kelvin=kelvin)


def v4_pixel(linear, applied: PictureOperations) -> tuple[float, ...]:
    """Picture operations v4's final pass, as it shipped (1c): the oracle for 'neutral is today'."""
    pixel = []
    for value, multiplier in zip(linear, applied.rgb_multipliers, strict=True):
        value = min(1.0, max(0.0, value * multiplier * applied.light_factor))
        value = (encode_srgb(value) - .5) * applied.contrast_slope + .5
        pixel.append(min(1.0, max(0.0, value) ** applied.gamma_exponent))
    return (*pixel, 1.0)


# -- 1. Neutral is today's code path (design rule 2) ---------------------------------------------

@pytest.mark.parametrize("applied", [
    operations(),
    operations(slope=.5), operations(slope=1.5),
    operations(exponent=18 / 22), operations(exponent=26 / 22),
    operations((.9, .95, 1.0), 1.2, .8, 24 / 22),
    operations((1.0, .8, .6), 1.5, 1.5, 26 / 22),       # brightness 150 %, the knee at 1: v4's clip
])
def test_neutral_print_look_draws_exactly_the_v4_pixels(applied):
    """Rule 2: with saturation 1, knee 1, floor 0 and Mat() every pixel is v4's, bit for bit, over a
    17^3 grid of inputs (sRGB steps of 1/16, in linear light). Mutation probe: compute step 2 at saturation 1 instead of skipping
    it -> some grid point moves by an ulp."""
    shade = mat_shadow(CoverFit(), SCREEN, Mat())
    assert shade is None
    grid = [decode_channel(index / 16) for index in range(17)]     # encoded steps, as photos hold them
    for linear in itertools.product(grid, repeat=3):
        assert final_pixel(linear, applied, inside=True, shade=shade) == v4_pixel(linear, applied), linear


# -- 2. The order, by a hand-worked table (design "The order") -----------------------------------

# Saturation 0.8, brightness 1.4, knee 0.6, contrast 1.2, gamma exponent 1, floor 2 % of white light
# and one shade 0.64, worked step by step (bright row): L = 0.2126*0.9 + 0.7152*0.45 + 0.0722*0.18 =
# 0.526176; saturated = L + 0.8 (c - L) = (0.8252352, 0.4652352, 0.2492352); x 1.4 = (1.1553293,
# 0.6513293, 0.3489293); M = 1.4, so t = (1.1553293 - 0.6)/0.4 = 1.3883232, w = 2, g = t (1 + t/4)
# / (1 + t) = 0.7830529, y = 0.6 + 0.4 g = 0.9132212, fade = g^2 = 0.6131719, each c y/x (1 - fade)
# + y fade = (0.9132212, 0.7591155, 0.6666520); white balance (1, 1, 1); sRGB encode, (e - 0.5)*1.2
# + 0.5, clamp = (1, 0.9626570, 0.9031986); in the panel's light (sRGB decode) 0.02 + 0.98 l, x 0.64,
# encode = the row. Dark row: 0.002 encodes to 0.0258, contrast takes it below 0, so the floor and
# the shade alone are left: encode(0.02 * 0.64).
PRINT = operations(light=1.4, slope=1.2, saturation=.8, knee=.6, floor=.02)
PRINT_TABLE = (
    ((0.9, 0.45, 0.18), (0.820979848329444, 0.7906092701711199, 0.7423245099634812)),
    ((0.18, 0.18, 0.18), (0.4564099910864193,) * 3),
    ((0.002, 0.002, 0.002), (0.11662871345570966,) * 3),
)


@pytest.mark.parametrize("linear, expected", PRINT_TABLE)
def test_the_print_look_runs_in_the_designs_order(linear, expected):
    """Order: saturation, brightness, rolloff, white balance, encode, contrast, gamma, then in the
    panel's light the black floor and the shade. Mutation probe: lift the floor before contrast ->
    the dark row is no longer the floor's alone."""
    assert final_pixel(linear, PRINT, inside=True, shade=(.64, .64, .64)) == pytest.approx(
        (*expected, 1.0), abs=1e-12)


# -- 3. The highlight rolloff (R2) ----------------------------------------------------------------

@pytest.mark.parametrize("knee, top", [(.18, 1.5), (.6, 1.4), (.9, 1.0 + 1e-6), (.5, 3.0)])
def test_the_rolloff_brings_the_peak_to_exactly_white_and_leaves_the_knee_smooth(knee, top):
    """The peak maps to exactly 1 (all channels: the path to white), the slope at the knee is 1,
    and below the knee nothing moves."""
    assert rolled_off((top, top * .4, 0.0), knee, top) == (1.0, 1.0, 1.0)
    h = 1e-7
    above = rolled_off((knee + h, 0.0, 0.0), knee, top)[0]
    assert (above - knee) / h == pytest.approx(1.0, abs=1e-6)
    below = (knee * .999, knee * .5, 0.01)
    assert rolled_off(below, knee, top) == below
    # Monotonic and hue-keeping on the way: a brighter input is never darker; channels stay ordered.
    previous = 0.0
    for step in range(1, 101):
        x = knee + (top - knee) * step / 100
        out = rolled_off((x, x / 2, x / 4), knee, top)
        assert out[0] >= previous and out[0] >= out[1] >= out[2]
        previous = out[0]


def test_the_rolloff_acts_only_where_white_passes_white():
    """M is brightness alone (design: the largest gain x brightness, the largest white-balance
    multiplier being 1): skipped (v4's clamp) at brightness <= 100 % whatever the saturation, or
    at knee 1; active above 100 %. Mutation probe: let saturation raise the peak -> the vivid case
    is not skipped."""
    assert rolloff_skipped(operations(knee=.5))
    assert rolloff_skipped(operations(light=.8, saturation=1.5, knee=.5))
    assert rolloff_skipped(operations(saturation=1.5, knee=.5))
    assert rolloff_skipped(operations(light=1.5, knee=1.0))
    assert not rolloff_skipped(operations(light=1.01, knee=.5))
    assert peak(operations((1.0, .5, .5), 1.2, saturation=1.5)) == 1.2


@pytest.mark.parametrize("saturation", [.5, 1.0, 1.2, 1.5])
def test_white_stays_white_at_full_brightness_whatever_the_saturation(saturation):
    """'White stays white' (design): at brightness 100 % paper white is exactly white for any
    saturation, with the rolloff at its strongest. Mutation probe: let saturation raise the peak ->
    white dims (0.95 at saturation 1.2)."""
    assert final_pixel((1.0, 1.0, 1.0), operations(saturation=saturation, knee=.18), inside=True) == pytest.approx(
        (1.0, 1.0, 1.0, 1.0), abs=1e-15)


@pytest.mark.parametrize("kelvin", [3500, 5000, 9300])
def test_the_rolloffs_white_is_the_displays_white(kelvin):
    """The colour temperature is the one home of white: with soft highlights on, white keeps the
    white balance's tint, and 100 % -> 101 % brightness moves a highlight by a hair, never from
    warm to neutral. Mutation probe: white balance before the rolloff -> 101 % paper white is
    (1, 1, 1) and the grey jumps."""
    balance = kelvin_white(kelvin)
    at, above = (operations(balance, light, knee=.5) for light in (1.0, 1.01))
    assert rolloff_skipped(at) and not rolloff_skipped(above)
    tinted = final_pixel((1.0, 1.0, 1.0), above, inside=True)[:3]
    assert tinted == pytest.approx(tuple(encode_srgb(channel) for channel in balance), abs=1e-12)
    for grey in (.8, .95, 1.0):
        below, over = (final_pixel((grey,) * 3, applied, inside=True) for applied in (at, above))
        assert over == pytest.approx(below, abs=.01), (grey, below, over)


def test_saturation_past_the_peak_ends_at_white_never_above():
    """A primary saturation pushes past white is held at the peak: exactly white, no channel
    above 1, and a less bright one keeps its hue."""
    vivid = operations(light=1.2, saturation=1.5, knee=.5)
    assert final_pixel((0.0, 0.0, 1.0), vivid, inside=True) == pytest.approx((1.0, 1.0, 1.0, 1.0), abs=1e-15)
    red = final_pixel((.5, 0.0, 0.0), vivid, inside=True)
    assert red[0] > red[1] and max(red[:3]) <= 1.0


# -- 4. The black floor is the last tone step (R10) -----------------------------------------------

def test_the_floor_lifts_black_keeps_white_and_never_reaches_outside_the_opening():
    """Black in -> exactly the floor; white stays white; outside every opening exactly black with
    the floor and a shadow on. Mutation probe: lift after the mask -> outside pixels non-zero."""
    lifted = operations(slope=1.5, exponent=26 / 22, floor=.05)
    assert final_pixel((0.0, 0.0, 0.0), lifted, inside=True) == pytest.approx((encode_srgb(.05),) * 3 + (1.0,),
                                                                             abs=1e-15)
    assert final_pixel((1.0, 1.0, 1.0), lifted, inside=True) == pytest.approx((1.0,) * 4, abs=1e-15)
    assert final_pixel((.3, .6, .9), lifted, inside=False, shade=(.5, .6, .7)) == (0.0, 0.0, 0.0, 1.0)
    fit = CoverFit(corners=((.2, .2), (.8, .2), (.8, .8), (.2, .8)))
    shadow = mat_shadow(fit, SCREEN, Mat(depth_mm=20, lights=(light(steep=5),)))
    for x, y in ((.1, .5), (.5, .1), (.9, .9), (.19, .5)):
        assert output_pixel(x, y, ((fit, 16 / 9, (.4, .4, .4), shadow),), lifted, (1920, 1080)) == (0, 0, 0, 1)


@pytest.mark.parametrize("light", [.01, .05, .18, .5])
def test_the_floor_is_black_point_compensation_in_the_panels_light(light):
    """2.5 % of white (matte paper): the panel shows share + (1 - share) * L for a pixel of light
    L, as a print with that black does, never a lift of the output value (which washes shadows
    out, x1.78 at L = 0.01). Mutation probe: lift the encoded value -> L = 0.01 shows 0.055, not
    0.035."""
    pixel = final_pixel((light,) * 3, operations(floor=.025), inside=True)[0]
    assert srgb_to_linear(pixel) == pytest.approx(.025 + .975 * light, abs=1e-12)


# -- 5-8. The mat shadow (R5, R6) ------------------------------------------------------------------

def test_no_depth_no_light_or_no_strength_casts_no_shadow():
    assert mat_shadow(CoverFit(), SCREEN, Mat()) is None
    assert mat_shadow(CoverFit(), SCREEN, Mat(depth_mm=0, lights=(light(),))) is None
    assert mat_shadow(CoverFit(), SCREEN, Mat(depth_mm=2, lights=(light(tenths=0),))) is None
    shadow = mat_shadow(CoverFit(), SCREEN, Mat(depth_mm=2, lights=(light(tenths=0), light())))
    assert len(shadow.lights) == 1                      # only lights that shine


def test_one_light_from_the_top_shades_a_band_its_depth_times_cot_steep_wide():
    """One light from 0, steep 45, no softness, strength 1.0, 6500 K, depth 2 mm, Cover at 0 on a
    27" 16:9 screen: 1 mm below the top edge is shaded to (1 + 0)/(1 + 1); 3 mm is lit; the bottom
    band is lit. At steep 60 the band is 2 cot 60 = 1.15 mm: 1 mm shaded, 1.5 mm lit. Mutation
    probe: use the depth as the width (no cot) -> the 1.5 mm point at steep 60 is shaded."""
    shadow = mat_shadow(CoverFit(), SCREEN, Mat(depth_mm=2, lights=(light(),)))
    assert shadow.size_mm == pytest.approx((WIDTH_MM, HEIGHT_MM))
    assert mat_shade(shadow, .5, 1 / HEIGHT_MM) == (.5, .5, .5)
    assert mat_shade(shadow, .5, 3 / HEIGHT_MM) == (1.0, 1.0, 1.0)
    assert mat_shade(shadow, .5, 1 - 1 / HEIGHT_MM) == (1.0, 1.0, 1.0)
    assert mat_shade(shadow, 1 / WIDTH_MM, .5) == (1.0, 1.0, 1.0)      # the side edges: lit
    steep = mat_shadow(CoverFit(), SCREEN, Mat(depth_mm=2, lights=(light(steep=60),)))
    assert mat_shade(steep, .5, 1 / HEIGHT_MM) == (.5, .5, .5)
    assert mat_shade(steep, .5, 1.5 / HEIGHT_MM) == (1.0, 1.0, 1.0)
    overhead = mat_shadow(CoverFit(), SCREEN, Mat(depth_mm=20, lights=(light(steep=90),)))
    assert mat_shade(overhead, .5, 1e-9) == (1.0, 1.0, 1.0)            # cot 90 is exactly 0
    # The shade is light: it multiplies the panel's light (sRGB, the panel the encode is for).
    assert final_pixel((1.0, 1.0, 1.0), operations(), inside=True, shade=(.5, .5, .5))[:3] == pytest.approx(
        (encode_srgb(.5),) * 3, abs=1e-15)


def test_a_soft_light_eases_its_edge_between_its_steepest_and_shallowest_rays():
    """Softness 20 at steep 45: fully dark nearer than 2 cot 55, fully lit from 2 cot 35, and a
    smoothstep between."""
    shadow = mat_shadow(CoverFit(), SCREEN, Mat(depth_mm=2, lights=(light(softness=20),)))
    near, far = (2 / math.tan(math.radians(a)) for a in (55, 35))
    assert (shadow.lights[0].dark_until_mm, shadow.lights[0].lit_from_mm) == pytest.approx((near, far))
    middle = mat_shade(shadow, .5, (near + far) / 2 / HEIGHT_MM)[0]
    assert middle == pytest.approx((1 + .5) / 2, abs=1e-9)
    assert mat_shade(shadow, .5, near * .99 / HEIGHT_MM) == (.5, .5, .5)
    assert mat_shade(shadow, .5, far * 1.01 / HEIGHT_MM) == (1.0, 1.0, 1.0)


@pytest.mark.parametrize("tilt", [7, 10, -10])
def test_a_light_from_above_shades_only_the_top_of_an_opening_level_on_the_wall(tilt):
    """A Cover Frame turned `tilt` degrees on the panel, its opening drawn level on the wall (so
    tilted on the output by the same angle): a light from straight above shades the opening's top
    edge and neither side. Mutation probe: turn the light by the whole rotation -> one side edge
    is shaded too (0.33 at 10 degrees)."""
    turn = math.radians(tilt)
    half_w, half_h, cx, cy = .3, .3, .5, .5
    corners = tuple((cx + x * math.cos(turn) - y * math.sin(turn) * 9 / 16,
                     cy + (x * math.sin(turn) * 16 / 9 + y * math.cos(turn)))
                    for x, y in ((-half_w, -half_h), (half_w, -half_h), (half_w, half_h), (-half_w, half_h)))
    fit = CoverFit(corners=corners, rotation=tilt % 360)
    shadow = mat_shadow(fit, SCREEN, Mat(depth_mm=8, lights=(light(steep=20),)))
    to_output = homography(fit.corners)
    edge = .002
    for u in (.2, .5, .8):
        assert mat_shade(shadow, *transform(to_output, u, edge))[0] < 1        # the top edge: shaded
    for v in (.3, .5, .7):
        for u in (edge, 1 - edge):                                             # the sides: lit
            assert mat_shade(shadow, *transform(to_output, u, v)) == (1.0, 1.0, 1.0), (u, v)


def test_a_light_from_wall_up_shades_the_edge_that_is_up_on_a_turned_frame():
    """Cover at 90: the picture's up (the wall's up) is the output's right edge, so a light from 0
    shades a band along the right edge, and the square's u extent is the picture's height.
    Mutation probe: ignore fit.rotation -> the top edge is shaded instead."""
    shadow = mat_shadow(CoverFit(rotation=90), SCREEN, Mat(depth_mm=2, lights=(light(),)))
    across, down = shadow.size_mm
    assert (across, down) == pytest.approx((HEIGHT_MM, WIDTH_MM))
    assert mat_shade(shadow, 1 - 1 / across, .5) == pytest.approx((.5, .5, .5))
    assert mat_shade(shadow, .5, 1 / down) == (1.0, 1.0, 1.0)
    assert mat_shade(shadow, 1 / across, .5) == (1.0, 1.0, 1.0)


def test_two_coloured_lights_tint_each_shadow_away_from_its_light():
    """R6: a cool window from the side (90, 7500 K) and a warm lamp from the top (0, 2700 K). Fully
    lit is exactly (1, 1, 1); in the window's shadow only the room and the lamp remain, so it reads
    warmer (red/blue > 1); under the top edge cooler; the corner, shaded by both, is darkest.
    Mutation probe: one denominator per light -> the centre is not exactly 1."""
    shadow = mat_shadow(CoverFit(), SCREEN, Mat(depth_mm=2, lights=(
        light(from_degrees=90, kelvin=7500), light(from_degrees=0, kelvin=2700))))
    cool, warm = light_colour(7500), light_colour(2700)
    assert shadow.lights[0].weight == cool
    assert mat_shade(shadow, .5, .5) == (1.0, 1.0, 1.0)
    side = mat_shade(shadow, 1 - 1 / WIDTH_MM, .5)
    top = mat_shade(shadow, .5, 1 / HEIGHT_MM)
    corner = mat_shade(shadow, 1 - 1 / WIDTH_MM, 1 / HEIGHT_MM)
    assert side[0] / side[2] > 1 and top[0] / top[2] < 1, (side, top)
    assert all(c < min(s, t) for c, s, t in zip(corner, side, top, strict=True)), (corner, side, top)
    assert corner == pytest.approx(tuple(1 / (1 + c + w) for c, w in zip(cool, warm, strict=True)))


@pytest.mark.parametrize("kelvin", [2000, 2700, 4000, 6500, 10000])
def test_a_lights_strength_is_its_brightness_and_its_kelvin_only_its_colour(kelvin):
    """Light.strength: 1.0 is as bright as all the room's other light, whatever its colour: each
    light's colour has luminance 1, so a full shadow from a light of strength 1 is half as bright
    in luminance at any kelvin. Mutation probe: use kelvin_white (largest channel 1) -> a 2700 K
    light is a third as bright as a 6500 K one."""
    colour = light_colour(kelvin)
    assert sum(weight * channel for weight, channel in zip(LUMA, colour, strict=True)) == pytest.approx(1.0)
    shadow = mat_shadow(CoverFit(), SCREEN, Mat(depth_mm=2, lights=(light(kelvin=kelvin),)))
    shaded = mat_shade(shadow, .5, 1 / HEIGHT_MM)
    lit = sum(weight * (1 + channel) for weight, channel in zip(LUMA, colour, strict=True))
    assert lit == pytest.approx(2.0)
    assert [shaded[c] * (1 + colour[c]) for c in range(3)] == pytest.approx([1.0] * 3)


def test_a_skewed_corners_opening_gets_an_even_band():
    """Lens 4: the edge distance is measured in mat space (the opening's own rectangle at its
    physical size), so the band under a skewed top edge is as wide at both ends. Mutation probe:
    measure in output fractions -> the two ends differ by more than 5 %."""
    skewed = CornersFit(corners=((.1, .1), (.9, .25), (.8, .9), (.15, .8)))
    shadow = mat_shadow(skewed, SCREEN, Mat(depth_mm=3, lights=(light(steep=30, softness=20),)))
    to_output = homography(skewed.corners)

    def band(u: float) -> float:
        """How far below the top edge (mm, mat space) the shade at column u crosses halfway."""
        low, high = 0.0, .5
        for _ in range(60):
            v = (low + high) / 2
            shade = mat_shade(shadow, *transform(to_output, u, v))[0]
            low, high = (v, high) if shade < .75 else (low, v)
        return low * shadow.size_mm[1]

    left, right = band(.05), band(.95)
    assert left == pytest.approx(right, rel=.01), (left, right)
    assert 3 / math.tan(math.radians(40)) < left < 3 / math.tan(math.radians(20))


def test_each_frame_on_an_output_carries_its_own_shadow():
    """output_pixel: the shade is the covering Frame's; a Frame with no shadow is unshaded."""
    a = CoverFit(corners=((.04, .1), (.46, .1), (.46, .9), (.04, .9)))
    b = CoverFit(corners=((.54, .1), (.96, .1), (.96, .9), (.54, .9)))
    shadow = mat_shadow(a, SCREEN, Mat(depth_mm=20, lights=(light(steep=5),)))
    frames = ((a, 16 / 9, (.5, .5, .5), shadow), (b, 16 / 9, (.5, .5, .5), None))
    plain = output_pixel(.75, .12, frames, operations(), (1920, 1080))
    shaded = output_pixel(.25, .12, frames, operations(), (1920, 1080))
    assert plain == final_pixel((.5, .5, .5), operations(), inside=True)
    assert shaded[0] < plain[0]
    assert shaded == final_pixel((.5, .5, .5), operations(), inside=True, shade=mat_shade(shadow, .25, .12))


# -- Step 1, the LUT (slice P2) ----------------------------------------------------------------------

def lut_table(colour) -> bytes:
    """A LUT body (contracts/lut.py layout) whose grid point (r, g, b), each 0..LUT_GRID-1, holds
    `colour(r, g, b)` as three bytes."""
    body = bytearray(LUT_BYTES)
    for r, g, b in itertools.product(range(LUT_GRID), repeat=3):
        offset = lut_offset(r, g, b)
        body[offset:offset + 3] = bytes(colour(r, g, b))
    return bytes(body)


IDENTITY = lut_table(lambda *point: (round(index * 255 / (LUT_GRID - 1)) for index in point))
INVERTING = lut_table(lambda *point: (255 - round(index * 255 / (LUT_GRID - 1)) for index in point))
WARM_SEPIA = lut_table(lambda r, g, b: (min(255, round((r + g + b) / 3 * 255 / 32 * 1.1)),
                                        round((r + g + b) / 3 * 255 / 32 * .8), round(b * 255 / 32 * .3)))
REF = LutRef("ab" * 32, "lut33-perceptual-relcol-bpc-1")


def with_lut(amount: float = 1.0, **fields) -> PictureOperations:
    return replace(operations(**fields), lut=LutMix(REF, amount))


def test_an_identity_lut_samples_back_its_input_and_an_inverting_one_mixes_to_mid_grey():
    """Trilinear at texel centres: the identity table returns its input within one byte step; an
    inverting table mixed in at 0.5 returns mid-grey (encoded 0.5)."""
    for encoded in itertools.product((0.0, .03, .2, .5, .51, .77, .999, 1.0), repeat=3):
        assert lut_sampled(IDENTITY, encoded) == pytest.approx(encoded, abs=1 / 255)
    for linear in ((0.0, 0.0, 0.0), (.2, .5, .9), (1.0, 1.0, 1.0)):
        mixed = lut_applied(linear, INVERTING, .5)
        assert [encode_srgb(value) for value in mixed] == pytest.approx([.5] * 3, abs=1 / 255)
    # Between grid points the blend is linear along each axis.
    halfway = (.5 / (LUT_GRID - 1), 0.0, 0.0)
    assert lut_sampled(INVERTING, halfway)[0] == pytest.approx((255 + INVERTING[3]) / 2 / 255)


def test_the_lut_comes_first_so_saturation_greys_the_tables_colour():
    """Order: the LUT acts on the photo as authored, then saturation. With saturation 0 the result
    is the grey of the table's output luminance. Mutation probe: apply the LUT after saturation ->
    the grey input comes out sepia, not grey."""
    linear = (.6, .3, .1)
    sampled = lut_applied(linear, WARM_SEPIA, 1.0)
    grey = sum(weight * value for weight, value in zip(LUMA, sampled, strict=True))
    pixel = final_pixel(linear, with_lut(saturation=0.0), inside=True, lut=WARM_SEPIA)
    assert pixel == pytest.approx((encode_srgb(grey),) * 3 + (1.0,), abs=1e-12)
    assert len(set(pixel[:3])) == 1


def test_a_lut_not_named_or_not_held_draws_exactly_the_sliders():
    """Rule 3's other half: no LUT, or one not yet held (the renderer draws on with the sliders),
    is exactly P1's pixel; a held one changes it."""
    for linear in ((.2, .5, .9), (1.0, .0, .3), (0.0, 0.0, 0.0)):
        sliders = final_pixel(linear, operations(light=1.2, saturation=1.3, knee=.5, floor=.05), inside=True)
        named = final_pixel(linear, with_lut(.8, light=1.2, saturation=1.3, knee=.5, floor=.05), inside=True)
        assert named == sliders
        assert final_pixel(linear, operations(), inside=True, lut=WARM_SEPIA) == final_pixel(
            linear, operations(), inside=True)                  # a table with no LutMix is never applied
    assert final_pixel((.2, .5, .9), with_lut(.8), inside=True, lut=WARM_SEPIA) != final_pixel(
        (.2, .5, .9), operations(), inside=True)
