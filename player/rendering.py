"""Platform-independent rendering boundary; no transport or media selection.

Native implementations must keep persistent output surfaces and invoke their GTK
and GStreamer objects only from their owning GLib thread. Every method is bounded:
preroll/presentation may report pending while native work continues asynchronously.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from contracts.models import Calibration, Layer, OutputBinding


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


class Renderer(Protocol):
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
        self.pending: set[str] = set()
        self.prepare_failures: set[str] = set()
        self.presentation_failures: set[str] = set()
        self.preparations: list[LocalLayer] = []
        self.presentations: list[OutputComposition] = []
        self.outputs: dict[str, OutputComposition] = {}
        self.resident: dict[str, LocalLayer] = {}

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
