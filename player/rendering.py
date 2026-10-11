"""Platform-independent rendering boundary; no transport or media selection.

Native implementations must keep persistent output surfaces and invoke their GTK
and GStreamer objects only from their owning GLib thread. Every method is bounded:
preroll/presentation may report pending while native work continues asynchronously.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from contracts.models import AppliedCalibration, Calibration, Layer, OutputBinding

REDRAW_RENEWAL = 1.0
"""Seconds after which a native renderer redraws an unchanged composition (render on change)."""
PRESENTATION_FRESHNESS = 2 * REDRAW_RENEWAL
"""Seconds a draw acknowledgment stays current: one missed renewal never reads as pending."""


@dataclass(frozen=True)
class LocalLayer:
    layer: Layer
    path: Path | None
    position: float
    alpha: float


@dataclass(frozen=True)
class OutputComposition:
    binding: OutputBinding
    calibration: Calibration
    layers: tuple[LocalLayer, ...] = ()
    fallback: bool = False


@dataclass(frozen=True)
class PrepareResult:
    status: Literal["prepared", "pending", "failed"]
    code: Literal["none", "decode", "capacity"] = "none"


@dataclass(frozen=True)
class CapacityResult:
    available: bool
    # A native adapter may set this only for a separately qualified device profile.
    qualified: bool = False


@dataclass(frozen=True)
class PresentationResult:
    status: Literal["presented", "pending", "failed"]
    code: Literal["none", "decode", "capacity"] = "none"
    # Native draw acknowledgment: the actual drawn snapshot and local monotonic
    # completion time. None denotes immediate candidate presentation (simulators).
    composition: OutputComposition | None = None
    presented_at: float | None = None
    # Logical composition stays independently authorized; this is the actual output transform.
    applied_calibration: AppliedCalibration | None = None


class Renderer(Protocol):
    gl_renderer: str | None  # GL_RENDERER once a GL context exists; None without GL

    def set_unbound_outputs(self, output_ids: tuple[str, ...], player_id: str | None,
                            central_link_state: Literal["connecting", "reachable", "retrying"] = "reachable",
                            configuration_received: bool = True) -> None: ...

    def set_identify_output(self, output_id: str | None) -> None: ...

    def prepare(self, layer: LocalLayer) -> PrepareResult: ...

    def capacity(self, compositions: tuple[OutputComposition, ...]) -> CapacityResult: ...

    def present(self, composition: OutputComposition) -> PresentationResult: ...

    def release(self, assignment_id: str) -> None: ...


class RecordingRenderer:
    """Deterministic simulator. Its default four-slot profile is not Pi evidence.

    ``pending`` and ``prepare_failures`` are controlled before preroll;
    ``presentation_failures`` exercises failure after successful preparation. A
    failed/pending presentation never replaces ``outputs``' successful composition.
    Capacity includes already-resident current pictures as well as replacements,
    all Outputs, overlays and retained reveal media.
    """

    def __init__(self, capacity: int = 4):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 0:
            raise ValueError("capacity must be a nonnegative integer")
        self.limit = capacity
        self.gl_renderer: str | None = None
        self.pending: set[str] = set()
        self.prepare_failures: set[str] = set()
        self.presentation_failures: set[str] = set()
        self.preparations: list[LocalLayer] = []
        self.presentations: list[OutputComposition] = []
        self.outputs: dict[str, OutputComposition] = {}
        self.resident: dict[str, LocalLayer] = {}
        self.unbound_outputs: tuple[str, ...] = ()
        self.enrolled_player_id: str | None = None
        self.central_link_state: Literal["connecting", "reachable", "retrying"] = "connecting"
        self.configuration_received = False
        self.central_link_history: list[str] = ["connecting"]
        self.identify_output: str | None = None

    def set_unbound_outputs(self, output_ids: tuple[str, ...], player_id: str | None,
                            central_link_state: Literal["connecting", "reachable", "retrying"] = "reachable",
                            configuration_received: bool = True) -> None:
        self.unbound_outputs = output_ids
        self.enrolled_player_id = player_id
        self.central_link_state = central_link_state
        self.configuration_received = configuration_received
        self.central_link_history.append(central_link_state)

    def set_identify_output(self, output_id: str | None) -> None:
        self.identify_output = output_id

    def prepare(self, layer: LocalLayer) -> PrepareResult:
        self.preparations.append(layer)
        key = layer.layer.assignment_id
        if key in self.prepare_failures:
            return PrepareResult("failed", "decode")
        if key in self.pending:
            self.resident[key] = layer
            return PrepareResult("pending")
        self.resident[key] = layer
        return PrepareResult("prepared")

    def capacity(self, compositions: tuple[OutputComposition, ...]) -> CapacityResult:
        # Current/replacement/reveal media may need concurrent decode resources.
        media = {
            local.layer.assignment_id
            for composition in (*self.outputs.values(), *compositions)
            for local in composition.layers
            if local.layer.variant is not None
        }
        media.update(key for key, local in self.resident.items() if local.layer.variant is not None)
        return CapacityResult(len(media) <= self.limit, qualified=False)

    def present(self, composition: OutputComposition) -> PresentationResult:
        keys = {local.layer.assignment_id for local in composition.layers}
        if keys & self.presentation_failures:
            return PresentationResult("failed", "decode")
        if keys & self.pending:
            return PresentationResult("pending")
        self.presentations.append(composition)
        self.outputs[composition.binding.output_id] = composition
        return PresentationResult("presented")

    def release(self, assignment_id: str) -> None:
        self.resident.pop(assignment_id, None)
