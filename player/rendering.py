"""Platform-independent rendering boundary; no transport or media selection.

A native implementation (player/plane_renderer.py) is driven only from its owning thread, the
Player's GLib main loop, where its screens, GPU context and decoders' handoffs live. Every method is
bounded: preroll/presentation may report pending while native work continues asynchronously.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from contracts.models import AppliedCalibration, Calibration, Layer, OutputBinding
from contracts.room_light import RoomLightsDocument


@dataclass(frozen=True)
class LocalLayer:
    layer: Layer
    path: Path | None
    # The layer's look when the composition was made (the planning tick's time, or the drawn
    # frame's in an acknowledgment); a renderer draws `player/look.py` `look` at its frame time.
    position: float
    alpha: float


@dataclass(frozen=True)
class OutputComposition:
    binding: OutputBinding
    layers: tuple[LocalLayer, ...] = ()
    fallback: bool = False
    # Plan time less this machine's monotonic time, so a renderer reads its layers at the time of
    # the frame it draws (player/look.py). None: a still composition, drawn as its layers say
    # (a fallback: black, or a kept photo at full strength).
    timebase: float | None = None

    @property
    def calibration(self) -> Calibration:
        """The Frame's calibration: its binding's, the one home of the fit a composition is drawn
        with, so a layer prepared ahead on the same binding is fitted as its frames draw it."""
        return self.binding.calibration

    @property
    def request(self) -> tuple:
        """What the composition asks to be shown, apart from how it looks at a time (each layer's
        position and alpha, the timebase): its binding (the calibration with it), whether it is a
        fallback, and each layer as planned (its timing, fades, priority and crop too) with its
        file, bottom to top. The one home of "the same composition" (`Renderer.present`; the
        executor waits for a draw by it, player/executor.py)."""
        return self.binding, self.fallback, tuple((local.layer, local.path) for local in self.layers)


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

    def set_player(self, player_id: str | None) -> None: ...   # the Player's registered id; None: none yet

    def set_room_lights(self, document: RoomLightsDocument) -> None: ...   # every Output draws with it

    # Prepared: ready to be drawn on `binding`'s Frame, whose fit and profile the plane renderer
    # finishes the layer's picture for ahead of its first frame (player/plane_renderer.py).
    def prepare(self, layer: LocalLayer, binding: OutputBinding) -> PrepareResult: ...

    def capacity(self, compositions: tuple[OutputComposition, ...]) -> CapacityResult: ...

    # "presented" answers `composition`'s request alone (`OutputComposition.request`), never what
    # was shown for another; one that cannot show it yet answers "pending".
    def present(self, composition: OutputComposition) -> PresentationResult: ...

    def release(self, assignment_id: str) -> None: ...


class UnavailableRenderer:
    """No picture can be made (player/renderer_root.py): registration remains possible without
    falsely acknowledging native output."""

    gl_renderer = None  # no GL context: nothing to report

    def set_player(self, player_id: str | None) -> None:
        pass

    def set_room_lights(self, document: RoomLightsDocument) -> None:
        pass

    def prepare(self, layer: LocalLayer, binding: OutputBinding) -> PrepareResult:
        return PrepareResult("failed", "capacity")

    def capacity(self, compositions: tuple[OutputComposition, ...]) -> CapacityResult:
        return CapacityResult(False)

    def present(self, composition: OutputComposition) -> PresentationResult:
        return PresentationResult("failed", "capacity")

    def release(self, assignment_id: str) -> None:
        pass


class RecordingRenderer:
    """Deterministic simulator. Its default four-slot profile is not Pi evidence.

    ``pending`` and ``prepare_failures`` are controlled before preroll;
    ``presentation_failures`` exercises failure after successful preparation. A
    failed/pending presentation never replaces ``outputs``' successful composition.
    Capacity counts, as a stand-in for the plane renderer's controller budget, every resident picture
    (current, replacement, overlay, retained, on any Output) plus the compositions asked
    about; a released picture frees its slot even while ``outputs`` still records it.
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
        self.player_id: str | None = None
        self.room_lights: list[RoomLightsDocument] = []     # every document drawn with, in order

    def set_player(self, player_id: str | None) -> None:
        self.player_id = player_id

    def set_room_lights(self, document: RoomLightsDocument) -> None:
        self.room_lights.append(document)

    def prepare(self, layer: LocalLayer, binding: OutputBinding) -> PrepareResult:
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
        media = {
            local.layer.assignment_id
            for composition in compositions
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
