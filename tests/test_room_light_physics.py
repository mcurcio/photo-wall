"""Room light's physics on the Pi (decision 0028), through the scalar reference the shader is held to
(player/geometry.py; tests/native_fit_pass_harness.py holds the fit pass's shader to it): the upgrade's
picture equals today's at neutral sliders, a light whose room light is absent, not reported,
unavailable, off or read against the wrong reference adds nothing (design rule 2), the bookshelf
spot's cone and its switch, and the dither that keeps a room dimmed to a few lux from banding."""
from __future__ import annotations

import math

import pytest

from contracts.frame_lighting import (
    AT_FULL,
    CloseLight,
    FarLight,
    FrameLighting,
    Paper,
    WashLight,
)
from contracts.models import PositionBox, frame_size_mm
from contracts.node_frame import neutral_operations
from contracts.node_picture import DisplayLight
from contracts.room_light import (
    Dimmer,
    FixedEntry,
    KelvinColour,
    Measured,
    MeasuredEntry,
    NotReported,
    ReportedEntry,
    RgbColour,
    RoomLightEntry,
    RoomLightsDocument,
)
from player.geometry import (
    HIGHLIGHT_KNEE,
    FrameLights,
    LitLight,
    dithered,
    encode_srgb,
    final_pixel,
    highlight_rolled,
    irradiance,
    light_on_frame,
    paper_reflectance,
    paper_signal,
    picture_lights,
)

MATCHED, BLACK = 1000.0, 0.0003
DISPLAY = DisplayLight(MATCHED, BLACK)
OPERATIONS = neutral_operations("headless", 1, DisplayLight(MATCHED, BLACK))
# The picture's physical size as it hangs: a 43" 16:9 picture, by hand.
WIDTH_MM, HEIGHT_MM = 43 * 25.4 * 16 / math.hypot(16, 9), 43 * 25.4 * 9 / math.hypot(16, 9)
SIZE = (WIDTH_MM, HEIGHT_MM)
UPGRADE_PAPER = Paper(whiteness=0.9, ink_density=-math.log10(BLACK))
WHITE_ROOM = FixedEntry(colour=KelvinColour(kelvin=6500))
SPOT = CloseLight(light="bookshelf-spot", lux=400, reference=AT_FULL, across_mm=0, up_mm=350, out_mm=250,
                  inner_beam_degrees=20, outer_beam_degrees=40)
WASH = WashLight(light="room", lux=50, reference=AT_FULL)


def lighting(*lights, paper: Paper = UPGRADE_PAPER, depth: float = 0.0) -> FrameLighting:
    return FrameLighting(mat_depth_mm=depth, lights=lights, paper=paper)


def document(**entries: RoomLightEntry) -> RoomLightsDocument:
    return RoomLightsDocument(change=1, lights={key.replace("_", "-"): value for key, value in entries.items()})


def pixel(photo, lit, x=.5, y=.5, operations=OPERATIONS):
    return final_pixel(photo, operations, inside=True, lights=lit, x=x, y=y)


@pytest.mark.parametrize("photo", [(0.0, 0.0, 0.0), (0.18, 0.18, 0.18), (0.9, 0.4, 0.05), (1.0, 1.0, 1.0)])
def test_the_upgrade_wash_and_paper_draw_todays_picture(photo):
    """Acceptance 2: one wash of a FULL_ON room light at the matched lux, paper of whiteness 0.9
    and ink -log10(black), neutral contrast, gamma and white: the signal is the photo."""
    lit = picture_lights(lighting(WashLight(light="room", lux=MATCHED, reference=AT_FULL)),
                       document(room=WHITE_ROOM), SIZE, DISPLAY)
    assert lit.peak == pytest.approx(1.0)
    for x, y in ((.1, .1), (.5, .5), (.9, .8)):
        assert pixel(photo, lit, x, y)[:3] == pytest.approx(tuple(encode_srgb(value) for value in photo), abs=1e-9)


@pytest.mark.parametrize("entry", [
    None,
    ReportedEntry(colour=KelvinColour(kelvin=3000), state=NotReported(), doubt=None),
    ReportedEntry(colour=KelvinColour(kelvin=3000), state=Dimmer(on=False, brightness=1.0), doubt=None),
    ReportedEntry(colour=RgbColour(rgb=(1.0, 0.2, 0.2)), state=Dimmer(on=False, brightness=0.7), doubt=None),
    MeasuredEntry(colour=KelvinColour(kelvin=3000), state=Measured(lux=120.0), doubt=None),  # AtFull against a Sensor
], ids=["absent", "not-reported", "off", "off-coloured", "measured-against-dimmer"])
def test_a_light_its_room_light_cannot_vouch_for_adds_nothing(entry):
    """Acceptance 3 (design rule 2): each adds exactly (0, 0, 0)."""
    assert light_on_frame(WASH, entry) == (0.0, 0.0, 0.0)
    assert light_on_frame(SPOT, entry) == (0.0, 0.0, 0.0)


def test_a_lux_reference_fits_only_a_sensor():
    measured = WashLight(light="room", lux=200, reference={"kind": "at-reading", "lux": 400.0})
    assert light_on_frame(measured, FixedEntry(colour=KelvinColour(kelvin=6500))) == (0.0, 0.0, 0.0)
    assert light_on_frame(measured, MeasuredEntry(colour=KelvinColour(kelvin=6500), state=Measured(lux=100.0), doubt=None)) == pytest.approx((50.0,) * 3)


def test_a_dimmer_scales_by_brightness_and_colours_at_luminance_one():
    warm = light_on_frame(WASH, ReportedEntry(colour=KelvinColour(kelvin=2700), state=Dimmer(on=True, brightness=0.5), doubt=None))
    assert 0.2126 * warm[0] + 0.7152 * warm[1] + 0.0722 * warm[2] == pytest.approx(25.0)
    assert warm[0] > warm[1] > warm[2]
    rgb = light_on_frame(WASH, ReportedEntry(colour=RgbColour(rgb=(1.0, 0.5, 0.0)), state=Dimmer(on=True, brightness=0.5), doubt=None))
    assert sum(weight * value for weight, value in zip((0.2126, 0.7152, 0.0722), rgb, strict=True)) == pytest.approx(25.0)
    assert rgb[0] > rgb[1] > rgb[2] == 0.0


def spot_lighting() -> FrameLighting:
    return lighting(SPOT, WashLight(light="room", lux=MATCHED / 10, reference=AT_FULL))


def test_the_spot_is_a_cone_that_feathers_and_switching_it_off_leaves_the_wash():
    """The tracer's Pi half, in the reference: the cone is brightest inside, falls through the
    feather and is gone outside; the spot off, the Frame equals the wash alone."""
    on = picture_lights(spot_lighting(), document(bookshelf_spot=WHITE_ROOM, room=WHITE_ROOM), SIZE, DISPLAY)
    off = picture_lights(spot_lighting(), document(bookshelf_spot=ReportedEntry(colour=KelvinColour(kelvin=6500), state=Dimmer(
        on=False, brightness=1.0), doubt=None), room=WHITE_ROOM), SIZE, DISPLAY)
    wash = picture_lights(lighting(WashLight(light="room", lux=MATCHED / 10, reference=AT_FULL)),
                        document(room=WHITE_ROOM), SIZE, DISPLAY)
    grey = (0.5, 0.5, 0.5)
    centre, feather, outside = (pixel(grey, on, x, .5)[0] for x in (.5, .64, .95))
    assert centre > feather > outside
    assert outside == pytest.approx(pixel(grey, wash, .95, .5)[0], abs=1e-12)
    for x, y in ((.5, .5), (.6, .4), (.95, .1)):
        assert pixel(grey, off, x, y) == pixel(grey, wash, x, y)


def test_the_highlights_above_white_roll_off_and_stay_continuous():
    assert highlight_rolled((0.5, 1.2, 2.0), 1.0) == (0.5, 1.2, 2.0)
    assert highlight_rolled((0.5, 0.3, HIGHLIGHT_KNEE), 3.0) == (0.5, 0.3, HIGHLIGHT_KNEE)
    rolled = highlight_rolled((3.0, 1.5, 0.3), 3.0)
    assert rolled == pytest.approx((1.0, 0.5, 0.1))
    middle = highlight_rolled((1.5, 0.75, 0.15), 3.0)
    assert HIGHLIGHT_KNEE < middle[0] < 1
    assert highlight_rolled((0.95, 0.5, 0.2), 1.0 + 1e-9) == pytest.approx((0.95, 0.5, 0.2), abs=1e-6)


def test_a_warm_light_keeps_one_colour_from_the_shadows_to_paper_white():
    """A print under one light has one colour at every tone: a grey ramp under a 2700 K wash bright
    enough to pass the TV's white keeps the same green/red and blue/red ratios after the rolloff.
    Mutation probe: roll each channel apart -> the ratios climb toward white (0.17 -> 0.51)."""
    warm = picture_lights(lighting(WashLight(light="lamp", lux=MATCHED, reference=AT_FULL)),
                        document(lamp=FixedEntry(colour=KelvinColour(kelvin=2700))), SIZE, DISPLAY)
    assert warm.peak > 1
    ratios = []
    for grey in (0.05, 0.18, 0.4, 0.7, 1.0):
        signal = highlight_rolled(paper_signal(paper_reflectance((grey,) * 3, UPGRADE_PAPER),
                                               irradiance(warm, .5, .5), DISPLAY), warm.peak)
        shown = [BLACK + (1 - BLACK) * value for value in signal]    # the light the panel gives, its glow included
        ratios.append((shown[1] / shown[0], shown[2] / shown[0]))
    for green, blue in ratios:
        assert green == pytest.approx(ratios[0][0], rel=0.01) and blue == pytest.approx(ratios[0][1], rel=0.05)


def test_the_close_light_falls_off_as_the_inverse_square_on_a_slant_and_feathers_smoothly():
    """R2 and R5 against numbers worked by hand: the spot alone (400 lx at the Frame's centre, 350 mm up
    and 250 mm out, 20 and 40 degree beams) gives (d0 / d)**3 x 400 inside its inner beam, the
    smoothstep of (outer - phi) / (outer - inner) of that in the feather, and nothing past the outer
    beam. Mutation probes: drop the (d0 / d)**3 -> the inner point reads 400; a linear feather -> the
    feather point misses the smoothstep; the picture's size taken as (H, W) -> the light's place moves."""
    lit = picture_lights(lighting(SPOT), document(bookshelf_spot=WHITE_ROOM), SIZE, DISPLAY)
    width, height = WIDTH_MM, HEIGHT_MM
    light = (width / 2, height / 2 - 350.0, 250.0)

    def expected(above_mm: float) -> float:
        point = (width / 2, height / 2 - above_mm, 0.0)
        aim = [c - at for c, at in zip((width / 2, height / 2, 0.0), light, strict=True)]
        ray = [p - at for p, at in zip(point, light, strict=True)]
        d0, d = math.hypot(*aim), math.hypot(*ray)
        phi = math.degrees(math.acos(sum(a * r for a, r in zip(aim, ray, strict=True)) / (d0 * d)))
        t = min(1.0, max(0.0, (20.0 - phi) / (20.0 - 10.0)))
        return 400.0 * (d0 / d) ** 3 * t * t * (3 - 2 * t)

    for above in (100.0, 150.0, 200.0):     # inside the inner beam, in the feather, past the outer
        measured = irradiance(lit, .5, .5 - above / height)
        assert measured == pytest.approx((expected(above),) * 3, rel=1e-9, abs=1e-9), above
    assert expected(100.0) > 700 and 0 < expected(150.0) < 400 and expected(200.0) == 0


def test_the_peak_is_the_brightest_paper_white_on_the_opening_not_a_bound_past_the_cone():
    """The rolloff's peak is where the paper white is brightest on the opening, the cone and the mat
    included: a spot close to the wall, its foot off the cone's aim, no longer overstates it many
    times over (the review's 22 against 3). Mutation probe: the nearest point's (d0 / d)**3 with no
    cone -> the peak is far above every pixel's signal."""
    close = CloseLight(light="bookshelf-spot", lux=400, reference=AT_FULL, across_mm=200, up_mm=0, out_mm=60,
                       inner_beam_degrees=20, outer_beam_degrees=40)
    lit = picture_lights(lighting(close), document(bookshelf_spot=WHITE_ROOM), SIZE, DISPLAY)
    white = (UPGRADE_PAPER.whiteness,) * 3
    brightest = max(max(paper_signal(white, irradiance(lit, (i + .5) / 101, (j + .5) / 101), DISPLAY))
                    for i in range(101) for j in range(101))
    assert brightest <= lit.peak * 1.05 and lit.peak <= brightest * 1.25, (lit.peak, brightest)


def test_below_the_panels_glow_the_signal_is_the_glow():
    assert paper_signal((0.01, 0.5, 0.9), (1.0, 1.0, 1.0), DISPLAY)[0] == 0.0


def test_a_ramp_dimmed_to_five_lux_dithers_instead_of_banding():
    """Acceptance 6: a smooth ramp under a 5 lx wash, quantised to 8 bits with `dithered`: no 4 x 4
    tile spans more than one code, and each tile's mean equals the undithered signal's within a
    sixth of a step (undithered rounding is off by up to half)."""
    dim = picture_lights(lighting(WashLight(light="room", lux=5, reference=AT_FULL)),
                       document(room=WHITE_ROOM), SIZE, DISPLAY)
    width = 256
    undithered = [pixel((i / (width - 1),) * 3, dim)[0] for i in range(width)]
    assert 8 < undithered[-1] * 255 < 40              # a few dozen steps: the room dims the picture
    worst = 0.0
    for left in range(0, width, 4):
        codes = [round(255 * min(1.0, max(0.0, dithered(undithered[left + dx], left + dx, dy))))
                 for dx in range(4) for dy in range(4)]
        assert max(codes) - min(codes) <= 1
        mean = sum(undithered[left:left + 4]) / 4 * 255
        worst = max(worst, abs(sum(codes) / 16 - mean))
    assert worst < 1 / 6


def test_the_dither_is_under_half_a_step_and_averages_out():
    offsets = [dithered(0.5, x, y) - 0.5 for x in range(4) for y in range(4)]
    assert max(abs(offset) for offset in offsets) < 0.5 / 255
    assert sum(offsets) == pytest.approx(0.0, abs=1e-15)
    assert dithered(0.5, 1, 2) == dithered(0.5, 5, 6)


# -- The Frame's size from its Display's (frame box run, bead fb-9) ------------------------------------

QN43 = (950, 540)                   # the Samsung's picture area, as its EDID base block states it
WINDOW = FarLight(light="window", lux=300, reference=AT_FULL, from_degrees=270, steep_degrees=45,
                  softness_degrees=0)   # from the left: its mat shadow runs down the opening's left edge
BAND_MM = 10.0                      # a 10 mm mat under a 45-degree light: 10 mm of shadow


def _window_lit(box: PositionBox, turn: int = 0):
    return picture_lights(lighting(WINDOW, depth=BAND_MM), document(window=WHITE_ROOM),
                          frame_size_mm(box, QN43, turn), DISPLAY)


def test_the_mat_shadow_is_drawn_at_the_frames_size_from_its_displays():
    """A Frame filling a 950 x 540 mm Display draws a far light's mat shadow at the width a 950 mm
    picture gives: dark to BAND_MM / 950 of the picture, lit past it (the scalar reference the
    shader is held to); a box half the screen wide is a 475 mm picture, so the same mat is twice the
    share of it. Mutation probe: `frame_size_mm` drops the box's width -> the half box's shadow
    ends at BAND_MM / 950."""
    whole = _window_lit(PositionBox())
    assert whole.size_mm == pytest.approx(QN43)
    edge = BAND_MM / QN43[0]
    assert irradiance(whole, .5 * edge, .5) == (0.0, 0.0, 0.0)
    assert irradiance(whole, 1.5 * edge, .5)[1] == pytest.approx(300.0)
    half = _window_lit(PositionBox(left=.25, top=0, width=.5, height=1))
    assert half.size_mm == pytest.approx((475.0, 540.0))
    assert irradiance(half, 1.5 * edge, .5) == (0.0, 0.0, 0.0)     # past the whole box's edge, still dark
    assert irradiance(half, 2.5 * edge, .5)[1] == pytest.approx(300.0)
    assert _window_lit(PositionBox(left=.25, top=0, width=.5, height=1), 90).size_mm == pytest.approx((270.0, 950.0))


def test_with_no_display_size_only_wash_lights_add_light():
    """A Display that states no size: the wash still lights the picture, everywhere the same; the
    far and the close light need millimetres and add none (frame box run decision 6). Mutation
    probe: drop the wash lights too when the size is None -> the wash reads nothing."""
    every = lighting(WASH, WINDOW, SPOT, depth=BAND_MM)
    room = document(room=WHITE_ROOM, window=WHITE_ROOM, bookshelf_spot=WHITE_ROOM)
    unknown = picture_lights(every, room, None, DISPLAY)
    assert unknown.size_mm is None and [lit.kind for lit in unknown.lights] == ["wash"]
    for x, y in ((0.0, 0.0), (.01, .5), (.5, .5), (1.0, 1.0)):
        assert irradiance(unknown, x, y)[1] == pytest.approx(50.0)
    assert unknown.peak == pytest.approx(max(paper_signal((UPGRADE_PAPER.whiteness,) * 3, (50.0,) * 3, DISPLAY)))
    assert [lit.kind for lit in picture_lights(every, room, QN43, DISPLAY).lights] == ["wash", "far", "close"]


def test_lights_with_no_size_hold_only_washes():
    close = LitLight("close", (1.0, 1.0, 1.0), position_mm=(0.0, 0.0, 100.0), beam=(.1, .2))
    with pytest.raises(ValueError, match="frame_lights"):
        FrameLights(size_mm=None, depth_mm=0.0, paper=UPGRADE_PAPER, lights=(close,), peak=1.0)
    FrameLights(size_mm=None, depth_mm=0.0, paper=UPGRADE_PAPER, lights=(LitLight("wash", (1.0, 1.0, 1.0)),),
                peak=1.0)
