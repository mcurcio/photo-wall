"""Frame lighting's rules (central/frame_lighting/policy.py): the check against the house, the
paper profile's recipe, the upgrade and new-Frame lighting, the trial envelope and which Frames name
what. Pure."""
from __future__ import annotations

import math

from central.frame_lighting.model import FrameLightingRefusal, LightingHouse
from central.frame_lighting.policy import (
    UPGRADE_LUX_MIN,
    UPGRADE_ROOM_LIGHT_ID,
    check,
    fits_trial_envelope,
    naming,
    new_frame_lighting,
    stamped,
    upgrade_lighting,
)
from central.room_light.model import FixedLight, MeasuredLight, ReportedLight, ReporterRef
from contracts.frame_lighting import (
    AT_FULL,
    SHEET_WHITENESS,
    AtReading,
    FrameLighting,
    Paper,
    WashLight,
)
from contracts.lut import LutRef
from contracts.node_picture import UPGRADE_DISPLAY_LIGHT
from contracts.room_light import FixedColour

WHITE = FixedColour(kelvin=4000)
PROFILE = "c" * 64
HOUSE = LightingHouse(lights={
    "spot": ReportedLight(name="Spot", reporter=ReporterRef(source="home-assistant", address="light.spot"), colour=WHITE),
    "window": MeasuredLight(name="Window", sensor="meter", colour=WHITE),
    "lamp": FixedLight(name="Lamp", colour=WHITE),
}, paper_profiles=frozenset({PROFILE}))


def lit(*lights: WashLight, profile: LutRef | None = None) -> FrameLighting:
    return FrameLighting(mat_depth_mm=0, lights=lights, paper=Paper(whiteness=0.9, ink_density=2.3, profile=profile))


def test_the_check_pairs_each_reference_with_its_room_light():
    """Mutation probe: AtFull accepted on a measured light -> the window at-full draft passes."""
    assert check(lit(WashLight(light="spot", lux=500.0, reference=AT_FULL),
                     WashLight(light="window", lux=80.0, reference=AtReading(lux=240.0)),
                     WashLight(light="lamp", lux=100.0, reference=AT_FULL)), HOUSE) is None
    assert check(lit(WashLight(light="cellar", lux=1.0, reference=AT_FULL)), HOUSE) is FrameLightingRefusal.UNKNOWN_ROOM_LIGHT
    assert check(lit(WashLight(light="window", lux=1.0, reference=AT_FULL)), HOUSE) is FrameLightingRefusal.REFERENCE_MISMATCH
    assert check(lit(WashLight(light="spot", lux=1.0, reference=AtReading(lux=2.0))), HOUSE) is (
        FrameLightingRefusal.REFERENCE_MISMATCH)
    assert check(lit(profile=LutRef("d" * 64, "r1")), HOUSE) is FrameLightingRefusal.UNKNOWN_PAPER_PROFILE
    assert check(lit(profile=LutRef(PROFILE, "r1")), HOUSE) is None


def test_a_profile_is_stamped_with_todays_recipe():
    assert stamped(lit(profile=LutRef(PROFILE, "old")), "today").paper.profile == LutRef(PROFILE, "today")
    assert stamped(lit(), "today") == lit()


def test_the_upgrade_matches_the_panel_and_keeps_the_old_brightness():
    lighting = upgrade_lighting(UPGRADE_DISPLAY_LIGHT, 2.5, 0.5)
    assert lighting.mat_depth_mm == 2.5
    assert lighting.lights == (WashLight(light=UPGRADE_ROOM_LIGHT_ID, lux=UPGRADE_DISPLAY_LIGHT.matched_lux * 0.5,
                                         reference=AT_FULL),)
    assert lighting.paper == Paper(whiteness=SHEET_WHITENESS, ink_density=-math.log10(UPGRADE_DISPLAY_LIGHT.black))
    assert upgrade_lighting(UPGRADE_DISPLAY_LIGHT, 0, 0).lights[0].lux == UPGRADE_LUX_MIN


def test_a_new_frame_views_the_first_fixed_light_or_none():
    """Mutation probe: the upgrade's "room" kept -> a house without it is named anyway."""
    new = new_frame_lighting(UPGRADE_DISPLAY_LIGHT, HOUSE)
    assert [light.light for light in new.lights] == ["lamp"] and check(new, HOUSE) is None
    bare = new_frame_lighting(UPGRADE_DISPLAY_LIGHT, LightingHouse(lights={}, paper_profiles=frozenset()))
    assert bare.lights == ()


def test_the_envelope_and_which_frames_name_what():
    assert fits_trial_envelope(upgrade_lighting(UPGRADE_DISPLAY_LIGHT, 0, 1))
    huge = lit().model_copy(update={"lights": tuple(
        WashLight(light="x" * 32, lux=0.123456789012345, reference=AtReading(lux=0.123456789012345))
        for _ in range(4))})
    assert fits_trial_envelope(huge)                  # the contract's longest lights fit, as its cap says
    named = naming([("hall", lit(WashLight(light="spot", lux=1.0, reference=AT_FULL), profile=LutRef(PROFILE, "r"))),
                    ("hall", lit(WashLight(light="lamp", lux=1.0, reference=AT_FULL))),
                    ("kitchen", lit(WashLight(light="spot", lux=1.0, reference=AT_FULL)))])
    assert named.lights == {"spot": ("hall", "kitchen"), "lamp": ("hall",)}
    assert named.paper_profiles == {PROFILE: ("hall",)}
