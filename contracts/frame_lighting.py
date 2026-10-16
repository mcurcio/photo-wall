"""A Frame's lighting: its view of the room lights, its mat and its paper. Physical properties in
real units: lux, kelvin (on the room light), millimetres, degrees and ink density. No room model:
every light is described from the Frame's view.

On the Frame's binding (contracts/models.py `OutputBinding.lighting`) beside its profile and
Position, edited live through Position's trial (contracts/node_calibration.py `TrialCandidate`); it
never changes the Frame output, so it never re-plans photos.

Three kinds of Frame light, each one Frame's view of one room light (several may view one room
light: a spotlight's cone and its spill), each with the lux it puts on the Frame (at its centre, for
a close light) while its room light is at `reference`:
- **wash**: even over the Frame, no mat shadow (a room's general light, a spot's spill);
- **far**: from far away at one angle, the whole Frame at once, casting the mat's shadow (a window);
- **close**: from a point near the Frame with a cone (the bookshelf spotlight): full brightness
  inside `inner_beam_degrees`, feathering to nothing at `outer_beam_degrees`, aimed at the Frame's
  centre.

`reference` scales lux in real units, picked by kind: `AtFull` for a light reported as a dimmer
(lux was read with it on at 100 %) or a fixed light, `AtReading(lux)` for a light a Sensor
measures (lux was read while the Sensor read `lux`). Which kind fits which room light is
central/frame_lighting's rule. What each field does to the picture is the Player's
(player/geometry.py, the scalar reference); this module holds shapes and ranges only.

The **paper** is the virtual print: how white it is (`whiteness`, a reflectance), how deep its ink
goes (`ink_density`, D-max: black reflects whiteness x 10^-ink_density), and an optional paper
profile baked to a LUT (contracts/lut.py `LutRef`; the only source of saturation).
"""
from __future__ import annotations

from typing import Annotated, Final, Literal, Self

from pydantic import Field, model_validator

from contracts.lut import LutRef
from contracts.room_light import LUX_MAX, RoomLightId
from contracts.wire_model import Model

FRAME_LIGHTS_MAX: Final = 4                     # a cap on the final pass's per-pixel cost, and on the trial's bytes
MAT_DEPTH_MM_MAX: Final = 20.0                  # allows exaggeration: a real flush 45-degree bevel casts millimetres
CLOSE_REACH_MM: Final = 5000.0                  # a close light sits within 5 m of the Frame's centre on each axis
BEAM_DEGREES_MAX: Final = 170.0                 # a close light's full beam angle (a bulb box's "beam angle")
WHITENESS_MIN: Final = 0.5                      # paper white's reflectance, WHITENESS_MIN..1
INK_DENSITY_MIN: Final = 0.5                    # D-max, INK_DENSITY_MIN..INK_DENSITY_MAX
INK_DENSITY_MAX: Final = 5.0                    # past any print, so a paper can match a panel's black exactly
GLOSS_INK: Final = 2.3                          # a gloss print's D-max (the paper presets)
MATTE_INK: Final = 1.6                          # a matte print's
SHEET_WHITENESS: Final = 0.9                    # the matching sheet's reflectance in the paper match: paper as
                                                # white as it, under the Display's matched lux, is full white

LuxOnFrame = Annotated[float, Field(gt=0, le=LUX_MAX, allow_inf_nan=False, title="LuxOnFrame")]   # above 0


class AtFull(Model):
    """`lux` was read with the room light on at 100 % (a dimmer, or a fixed light)."""
    kind: Literal["at-full"] = "at-full"


class AtReading(Model):
    """`lux` was read while the room light's Sensor read `lux` (a measured light)."""
    kind: Literal["at-reading"] = "at-reading"
    lux: LuxOnFrame


Reference = Annotated[AtFull | AtReading, Field(discriminator="kind")]
AT_FULL: Final = AtFull()
Millimetres = Annotated[float, Field(ge=-CLOSE_REACH_MM, le=CLOSE_REACH_MM, allow_inf_nan=False)]


class WashLight(Model):
    """Even over the Frame; no mat shadow."""
    kind: Literal["wash"] = "wash"
    light: RoomLightId
    lux: LuxOnFrame
    reference: Reference


class FarLight(Model):
    """From far away at one angle, falling on the whole Frame at once; casts the mat's shadow."""
    kind: Literal["far"] = "far"
    light: RoomLightId
    lux: LuxOnFrame
    reference: Reference
    from_degrees: int = Field(ge=0, le=359)        # clockwise from the wall's up (0 above, 90 from the right);
                                                   # wall-referenced through the Frame's rotation
    steep_degrees: int = Field(ge=5, le=90)        # off the wall: low is grazing (a long shadow), 90 straight on (none)
    softness_degrees: int = Field(ge=0, le=45)     # the light's apparent size: a bare bulb ~1, a window ~30


class CloseLight(Model):
    """From a point near the Frame, aimed at its centre, in a cone: `lux` is at the Frame's centre."""
    kind: Literal["close"] = "close"
    light: RoomLightId
    lux: LuxOnFrame
    reference: Reference
    across_mm: Millimetres                         # from the Frame's centre along the wall, + right
    up_mm: Millimetres                             # + up
    out_mm: float = Field(gt=0, le=CLOSE_REACH_MM, allow_inf_nan=False)   # out from the wall
    inner_beam_degrees: float = Field(gt=0, lt=BEAM_DEGREES_MAX, allow_inf_nan=False)   # full brightness inside
    outer_beam_degrees: float = Field(gt=0, le=BEAM_DEGREES_MAX, allow_inf_nan=False)   # feathers to nothing here

    @model_validator(mode="after")
    def beam(self) -> Self:
        if self.inner_beam_degrees >= self.outer_beam_degrees:
            raise ValueError("inner beam must be narrower than the outer beam")
        return self


FrameLight = Annotated[WashLight | FarLight | CloseLight, Field(discriminator="kind")]


class Paper(Model):
    """The virtual print. Black is whiteness x 10^-ink_density of the light falling on it."""
    whiteness: float = Field(ge=WHITENESS_MIN, le=1, allow_inf_nan=False)
    ink_density: float = Field(ge=INK_DENSITY_MIN, le=INK_DENSITY_MAX, allow_inf_nan=False)
    profile: LutRef | None = None                  # a paper profile baked to a LUT (contracts/lut.py)


class FrameLighting(Model):
    """A Frame's lights, mat and paper. A Frame light whose room light the room-lights document does
    not carry, or carries as unavailable or not reported, adds no light."""
    mat_depth_mm: float = Field(ge=0, le=MAT_DEPTH_MM_MAX, multiple_of=0.5, allow_inf_nan=False)
    lights: tuple[FrameLight, ...] = Field(max_length=FRAME_LIGHTS_MAX)
    paper: Paper


__all__ = [
    "AT_FULL", "BEAM_DEGREES_MAX", "CLOSE_REACH_MM", "FRAME_LIGHTS_MAX", "GLOSS_INK", "INK_DENSITY_MAX",
    "INK_DENSITY_MIN", "MATTE_INK", "MAT_DEPTH_MM_MAX", "SHEET_WHITENESS", "WHITENESS_MIN", "AtFull", "AtReading",
    "CloseLight", "FarLight", "FrameLight", "FrameLighting", "LuxOnFrame", "Paper", "Reference", "WashLight",
]
