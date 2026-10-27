"""Central's errors' operator API shapes (the console's Needs attention source rows and the Frame
page's "Central's error"). Instants are Central's clock (seconds since the epoch)."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from central.room_light.model import Address, Source
from central.room_light.views import DoubtView
from contracts.identifiers import Identifier
from contracts.models import Model
from contracts.node_impeded import Impeded
from contracts.room_light import RoomLightId


class SourceAttentionView(Model):
    """One reference in doubt now (room lights alone: no Pi needed): its source and address, its
    doubt, the room lights it reports, the Frames whose lighting names those lights, and what to do
    (its source's SourceAdvice, one line in the owner's terms)."""
    source: Source
    address: Address
    doubt: DoubtView
    lights: tuple[RoomLightId, ...]
    frames: tuple[Identifier, ...]
    what_to_do: str


class CentralErrorsView(Model):
    """Every reference in doubt now, in House › Lights order."""
    in_doubt: tuple[SourceAttentionView, ...]


class FrameNotReported(Model):
    """The Frame's Player never said (an older Pi, or never attached)."""
    kind: Literal["not-reported"] = "not-reported"


class FrameClear(Model):
    """The Frame's Player says no doubt blacks it."""
    kind: Literal["clear"] = "clear"


class FrameImpeded(Model):
    """The Frame's Player says Central's doubt blacks it: its report's entry, verbatim."""
    kind: Literal["impeded"] = "impeded"
    by: Impeded


FrameImpediment = Annotated[FrameNotReported | FrameClear | FrameImpeded, Field(discriminator="kind", title="FrameImpediment")]


class FrameImpedimentView(Model):
    """What a Frame's Player last said about Central's error on it."""
    frame_id: Identifier
    impediment: FrameImpediment


__all__ = ["CentralErrorsView", "FrameClear", "FrameImpeded", "FrameImpediment", "FrameImpedimentView",
           "FrameNotReported", "SourceAttentionView"]
