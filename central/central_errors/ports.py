"""What Central's errors depend on and offer: one plain method per question."""
from __future__ import annotations

from typing import Protocol

from central.central_errors.views import CentralErrorsView, FrameImpedimentView
from central.room_light.model import ColourSensorRef, DoubtReason, ReporterRef, SensorRef
from contracts.identifiers import TargetIdentifier

Reference = ReporterRef | SensorRef | ColourSensorRef   # an opaque (source, address); its source's adapter reads it


class SourceAdvice(Protocol):
    """Implemented by each reporting source's adapter (central/infra/home_assistant/advice.py, the Pi
    meter's in central/infra/light_sensor_judge.py); Central's errors dispatch by the reference's
    source, never branch on it."""

    def what_to_do(self, reference: Reference, reason: DoubtReason) -> str:
        """One line in the owner's terms for `reference` in doubt for `reason`. Home Assistant:
        cant-read + update-galleria -> "Update Galleria in Home Assistant (HACS) to its newest release";
        cant-read + update-central -> "Update Photo Wall (Central)"; cant-read + matched -> "Photo Wall
        can't read this Galleria: report a bug"; cant-tell -> "Check {entity} in Home Assistant";
        link-closed -> "Check Home Assistant and its Galleria integration". A Pi's meter: cant-read ->
        "Install the current release on Player p-…"; cant-tell -> "Check the light meter on Player p-…"."""
        ...


class CentralErrors(Protocol):
    """Injected into Needs attention's and the Frame page's routes."""

    def in_doubt(self) -> CentralErrorsView:
        """Every reference in doubt now, with its lights, the Frames naming them and what to do. A
        store failure is raised: never "nothing needs attention"."""
        ...

    def impeded(self, frame_id: TargetIdentifier) -> FrameImpedimentView:
        """What `frame_id`'s Player last said on its `impeded` key (contracts/node_impeded.py), for the
        Frame's current binding."""
        ...


__all__ = ["CentralErrors", "Reference", "SourceAdvice"]
