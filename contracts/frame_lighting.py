"""A Frame's lighting: its view of the room lights, its mat and its paper (decision 0028; it replaces
the print look's Mat on the Frame's binding). Physical properties in real units (owner,
2026-10-15: "the Frame configuration as a set of physical properties and let the shader act as a
sort of "physics engine""; "real units"): lux, kelvin (on the room light), millimetres, degrees and
ink density. No room model: every light is described from the Frame's view ("3d space might be too
complex without a full 3d model of the space", owner, 2026-10-15).

On the Frame's binding (contracts/models.py `OutputBinding.lighting`) beside its profile and
Position, saved through Position's trial (contracts/node_calibration.py `lighting_json`); it never
changes the Frame output, so it never re-plans photos.

Three kinds of Frame light, each one Frame's view of one room light (several may view one room
light: a spotlight's cone and its spill), each with the lux it puts on the Frame (at its centre, for
a close light) while its room light is at `reference`:
- **wash**: even over the Frame, no mat shadow (a room's general light, a spot's spill);
- **far**: from far away at one angle, the whole Frame at once, casting the mat's shadow (a
  window; today's mat light, with lux for its relative strength);
- **close**: from a point near the Frame with a cone (the bookshelf spotlight): full brightness
  inside `inner_beam_degrees`, feathering to nothing at `outer_beam_degrees`, aimed at the Frame's
  centre.

`reference` scales lux in real units: DIMMER_AT_FULL for a light a dimmer reports (lux was read
with it on at 100 %), or the Sensor's lux reading when lux was read (a measured light). What each
field does to the picture is the Player's (player/geometry.py, the scalar reference); this module
holds shapes and ranges only.

The **paper** is the virtual print (owner, 2026-10-15, Q3: the paper lives on the Frame): how white
it is (`whiteness`, a reflectance), how deep its ink goes (`ink_density`, D-max: black reflects
whiteness x 10^-ink_density), and an optional paper profile baked to a LUT (contracts/lut.py
`LutRef`; the only source of saturation, owner, 2026-10-15, Q2).
"""
from __future__ import annotations

from typing import Annotated, Final, Literal, Self

from pydantic import Field, model_validator

from contracts.lut import LutRef
from contracts.room_light import LUX_MAX, ROOM_LIGHT_ID_PATTERN
from contracts.wire_model import Model

DIMMER_AT_FULL: Final = "dimmer-at-full"        # a reference: the room light on at 100 %
FRAME_LIGHTS_MAX: Final = 4                     # a cap on the final pass's per-pixel cost, and on the trial's bytes
MAT_DEPTH_MM_MAX: Final = 20.0                  # allows exaggeration: a real flush 45-degree bevel casts millimetres
CLOSE_REACH_MM: Final = 5000.0                  # a close light sits within 5 m of the Frame's centre on each axis
BEAM_DEGREES_MAX: Final = 170.0                 # a close light's full beam angle (a bulb box's "beam angle")
WHITENESS_MIN: Final = 0.5                      # paper white's reflectance, WHITENESS_MIN..1
INK_DENSITY_MIN: Final = 0.5                    # D-max, INK_DENSITY_MIN..INK_DENSITY_MAX
INK_DENSITY_MAX: Final = 5.0                    # past any print, so a paper can match a panel's black exactly
GLOSS_INK: Final = 2.3                          # the console's presets
MATTE_INK: Final = 1.6
SHEET_WHITENESS: Final = 0.9                    # the matching sheet's reflectance in the paper match: paper as
                                                # white as it, under the Display's matched lux, is full white

RoomLightId = Annotated[str, Field(pattern=rf"^{ROOM_LIGHT_ID_PATTERN}$")]
Lux = Annotated[float, Field(gt=0, le=LUX_MAX, allow_inf_nan=False)]
Reference = Lux | Literal["dimmer-at-full"]
Millimetres = Annotated[float, Field(ge=-CLOSE_REACH_MM, le=CLOSE_REACH_MM, allow_inf_nan=False)]


class WashLight(Model):
    """Even over the Frame; no mat shadow."""
    kind: Literal["wash"] = "wash"
    light: RoomLightId
    lux: Lux
    reference: Reference


class FarLight(Model):
    """From far away at one angle, falling on the whole Frame at once; casts the mat's shadow."""
    kind: Literal["far"] = "far"
    light: RoomLightId
    lux: Lux
    reference: Reference
    from_degrees: int = Field(ge=0, le=359)        # clockwise from the wall's up (0 above, 90 from the right);
                                                   # wall-referenced through the Frame's rotation
    steep_degrees: int = Field(ge=5, le=90)        # off the wall: low is grazing (a long shadow), 90 straight on (none)
    softness_degrees: int = Field(ge=0, le=45)     # the light's apparent size: a bare bulb ~1, a window ~30


class CloseLight(Model):
    """From a point near the Frame, aimed at its centre, in a cone: `lux` is at the Frame's centre."""
    kind: Literal["close"] = "close"
    light: RoomLightId
    lux: Lux
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
    not carry, or carries as unavailable or not reported, adds no light (design rule 2)."""
    mat_depth_mm: float = Field(ge=0, le=MAT_DEPTH_MM_MAX, multiple_of=0.5, allow_inf_nan=False)
    lights: tuple[FrameLight, ...] = Field(max_length=FRAME_LIGHTS_MAX)
    paper: Paper


__all__ = [
    "BEAM_DEGREES_MAX", "CLOSE_REACH_MM", "DIMMER_AT_FULL", "FRAME_LIGHTS_MAX", "GLOSS_INK", "INK_DENSITY_MAX",
    "INK_DENSITY_MIN", "MATTE_INK", "MAT_DEPTH_MM_MAX", "SHEET_WHITENESS", "WHITENESS_MIN", "CloseLight",
    "FarLight", "FrameLight", "FrameLighting", "Lux", "Paper", "Reference", "RoomLightId", "WashLight",
]
