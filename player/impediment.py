"""The Pi's judge of "impedes" (DESIGN-r8 §5, Q2 C; BUILD.md, owner 2026-10-27: "1&2&3 all have
the same answer: when the issue induces blackness on the frame"). Pure: no I/O, no clock.

A Frame is impeded when the light the Player drew at its centre is exactly 0 lx (black) AND at least
one room light its lighting names carries a doubt (contracts/room_light.py `CentralWords`) in the
room-lights document it drew with. A dim but non-black Frame is never impeded. It reads only what
the Pi holds: the Frame's lighting, the document it drew with and the light it drew; never Central's
reachability, never an age. The Player draws exactly what the document says (the last stated value
stands); the judge changes no drawing.

The Player judges every Frame it draws at every main-loop turn (player/service.py
`impeded_frames`) and writes the result whole on its line (player/player_line.py `put_impeded`,
IMPEDED_KEY), restated at every attach and on every change.
"""
from __future__ import annotations

from contracts.frame_lighting import FrameLighting
from contracts.identifiers import TargetIdentifier
from contracts.node_impeded import Impeded
from contracts.node_state import OutputId
from contracts.room_light import Lux, MeasuredEntry, ReportedEntry, RoomLightsDocument


def impeded(frame_id: TargetIdentifier, output_id: OutputId, grant_id: str, lighting: FrameLighting,
            drawn_with: RoomLightsDocument, drawn_lux: Lux) -> Impeded | None:
    """The Frame's impediment now, or None. Impeded only when `drawn_lux` == 0 and one or more room
    lights `lighting` names carry a doubt in `drawn_with`: `lights` those lights in id order, `words`
    the first one's CentralWords verbatim, `grant_id` the shell grant the Frame was drawn under."""
    if drawn_lux != 0:
        return None
    doubted = {}
    for light in lighting.lights:
        entry = drawn_with.lights.get(light.light)
        if isinstance(entry, ReportedEntry | MeasuredEntry) and entry.doubt is not None:
            doubted[light.light] = entry.doubt
    if not doubted:
        return None
    lights = tuple(sorted(doubted))
    return Impeded(frame_id=frame_id, output_id=output_id, grant_id=grant_id, lights=lights,
                   words=doubted[lights[0]])


__all__ = ["impeded"]
