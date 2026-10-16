"""Room light's physics on the Pi (decision 0028), through the scalar reference the shader is held to
(player/geometry.py; tests/native_renderer_pixel_harness.py holds the shader to it): the upgrade's
picture equals today's at neutral sliders, a light whose room light is absent, not reported,
unavailable, off or read against the wrong reference adds nothing (design rule 2), the bookshelf
spot's cone and its switch, and the dither that keeps a room dimmed to a few lux from banding."""
from __future__ import annotations

import math

import pytest

from contracts.frame_lighting import (
    DIMMER_AT_FULL,
    CloseLight,
    FrameLighting,
    Paper,
    WashLight,
)
from contracts.models import CoverFit, FrameProfile
from contracts.node_frame import neutral_operations
from contracts.node_picture import DisplayLight
from contracts.room_light import (
    FULL_ON,
    Dimmer,
    FixedColour,
    Measured,
    NotReported,
    ReportedColour,
    RoomLightEntry,
    RoomLightsDocument,
    Unavailable,
)
from player.geometry import (
    HIGHLIGHT_KNEE,
    dithered,
    encode_srgb,
    final_pixel,
    frame_lights,
    highlight_rolled,
    light_on_frame,
    paper_signal,
)

MATCHED, BLACK = 1000.0, 0.0003
DISPLAY = DisplayLight(MATCHED, BLACK)
OPERATIONS = neutral_operations("headless", 1, MATCHED, BLACK)
FIT = CoverFit(corners=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)))
PROFILE = FrameProfile(width_px=1600, height_px=900, diagonal_inches=43)
UPGRADE_PAPER = Paper(whiteness=0.9, ink_density=-math.log10(BLACK))
WHITE_ROOM = RoomLightEntry(FixedColour(6500), FULL_ON)
SPOT = CloseLight(light="bookshelf-spot", lux=400, reference=DIMMER_AT_FULL, across_mm=0, up_mm=350, out_mm=250,
                  inner_beam_degrees=20, outer_beam_degrees=40)
WASH = WashLight(light="room", lux=50, reference=DIMMER_AT_FULL)


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
    lit = frame_lights(lighting(WashLight(light="room", lux=MATCHED, reference=DIMMER_AT_FULL)),
                       document(room=WHITE_ROOM), FIT, PROFILE, DISPLAY)
    assert lit.peak == pytest.approx(1.0)
    for x, y in ((.1, .1), (.5, .5), (.9, .8)):
        assert pixel(photo, lit, x, y)[:3] == pytest.approx(tuple(encode_srgb(value) for value in photo), abs=1e-9)


@pytest.mark.parametrize("entry", [
    None,
    RoomLightEntry(FixedColour(3000), NotReported()),
    RoomLightEntry(FixedColour(3000), Unavailable()),
    RoomLightEntry(FixedColour(3000), Dimmer(on=False, brightness=1.0)),
    RoomLightEntry(ReportedColour((0.3, 0.5, 0.2)), Dimmer(on=False, brightness=0.7, rgb=(1.0, 0.2, 0.2))),
    RoomLightEntry(FixedColour(3000), Measured(lux=120.0)),            # DIMMER_AT_FULL against a Sensor
], ids=["absent", "not-reported", "unavailable", "off", "off-coloured", "measured-against-dimmer"])
def test_a_light_its_room_light_cannot_vouch_for_adds_nothing(entry):
    """Acceptance 3 (design rule 2): each adds exactly (0, 0, 0)."""
    assert light_on_frame(WASH, entry) == (0.0, 0.0, 0.0)
    assert light_on_frame(SPOT, entry) == (0.0, 0.0, 0.0)


def test_a_lux_reference_fits_only_a_sensor():
    measured = WashLight(light="room", lux=200, reference=400.0)
    assert light_on_frame(measured, RoomLightEntry(FixedColour(6500), FULL_ON)) == (0.0, 0.0, 0.0)
    assert light_on_frame(measured, RoomLightEntry(FixedColour(6500), Measured(lux=100.0))) == pytest.approx((50.0,) * 3)


def test_a_dimmer_scales_by_brightness_and_colours_at_luminance_one():
    warm = light_on_frame(WASH, RoomLightEntry(FixedColour(2700), Dimmer(on=True, brightness=0.5)))
    assert 0.2126 * warm[0] + 0.7152 * warm[1] + 0.0722 * warm[2] == pytest.approx(25.0)
    assert warm[0] > warm[1] > warm[2]
    shares = (0.3, 0.6, 0.1)
    rgb = light_on_frame(WASH, RoomLightEntry(ReportedColour(shares), Dimmer(on=True, brightness=0.5, rgb=(1.0, 0.5, 0.0))))
    assert sum(weight * value for weight, value in zip((0.2126, 0.7152, 0.0722), rgb, strict=True)) == pytest.approx(
        50 * 0.5 * (1.0 * 0.3 + 0.5 * 0.6))
    assert rgb[2] == 0.0


def spot_lighting() -> FrameLighting:
    return lighting(SPOT, WashLight(light="room", lux=MATCHED / 10, reference=DIMMER_AT_FULL))


def test_the_spot_is_a_cone_that_feathers_and_switching_it_off_leaves_the_wash():
    """The tracer's Pi half, in the reference: the cone is brightest inside, falls through the
    feather and is gone outside; the spot off, the Frame equals the wash alone."""
    on = frame_lights(spot_lighting(), document(bookshelf_spot=WHITE_ROOM, room=WHITE_ROOM), FIT, PROFILE, DISPLAY)
    off = frame_lights(spot_lighting(), document(bookshelf_spot=RoomLightEntry(FixedColour(6500), Dimmer(
        on=False, brightness=1.0)), room=WHITE_ROOM), FIT, PROFILE, DISPLAY)
    wash = frame_lights(lighting(WashLight(light="room", lux=MATCHED / 10, reference=DIMMER_AT_FULL)),
                        document(room=WHITE_ROOM), FIT, PROFILE, DISPLAY)
    grey = (0.5, 0.5, 0.5)
    centre, feather, outside = (pixel(grey, on, x, .5)[0] for x in (.5, .64, .95))
    assert centre > feather > outside
    assert outside == pytest.approx(pixel(grey, wash, .95, .5)[0], abs=1e-12)
    for x, y in ((.5, .5), (.6, .4), (.95, .1)):
        assert pixel(grey, off, x, y) == pixel(grey, wash, x, y)


def test_the_highlights_above_white_roll_off_and_stay_continuous():
    assert highlight_rolled((0.5, 1.2, 2.0), 1.0) == (0.5, 1.2, 2.0)
    rolled = highlight_rolled((0.5, HIGHLIGHT_KNEE, 1.5, 3.0), 3.0)
    assert rolled[:2] == (0.5, HIGHLIGHT_KNEE) and HIGHLIGHT_KNEE < rolled[2] < 1 and rolled[3] == 1.0
    assert highlight_rolled((0.95,), 1.0 + 1e-9)[0] == pytest.approx(0.95, abs=1e-6)


def test_below_the_panels_glow_the_signal_is_the_glow():
    assert paper_signal((0.01, 0.5, 0.9), (1.0, 1.0, 1.0), DISPLAY)[0] == 0.0


def test_a_ramp_dimmed_to_five_lux_dithers_instead_of_banding():
    """Acceptance 6: a smooth ramp under a 5 lx wash, quantised to 8 bits with `dithered`: no 4 x 4
    tile spans more than one code, and each tile's mean equals the undithered signal's within a
    sixth of a step (undithered rounding is off by up to half)."""
    dim = frame_lights(lighting(WashLight(light="room", lux=5, reference=DIMMER_AT_FULL)),
                       document(room=WHITE_ROOM), FIT, PROFILE, DISPLAY)
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
