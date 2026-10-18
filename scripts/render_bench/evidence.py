"""The tracer's verdict, pure (bead B6): each recorded moment held to what it must show."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from scripts.render_bench.kms_state import KmsPlane
from scripts.render_bench.registers import RegisterSample

ALPHA_TOLERANCE: Final = 1 / 255    # a strength and the kernel's 16-bit alpha agree within one level
GPU_IDLE: Final = 0.05              # the GPU's busy share that counts as idle


@dataclass(frozen=True)
class Moment:
    """One sample of the run: plan time `at`, the step's name, the strengths the rule gives
    (assignment id -> strength, timeline.py `strengths_at`), the kernel's planes then, the register
    sample, whether a picture was being finished or a video played since the last moment
    (`drawing`), and the GPU's busy share since the last moment (v3d debugfs)."""
    at: float
    step: str
    expected: Mapping[str, float]
    planes: tuple[KmsPlane, ...]
    registers: RegisterSample
    drawing: bool
    gpu_busy: float


@dataclass(frozen=True)
class Verdict:
    """The tracer's checks (s1-page.md, the tracer): every expected picture on its own plane above
    the screen's primary; each plane's alpha within ALPHA_TOLERANCE of its strength and its blend
    mode Pre-multiplied; no glitch; the GPU below GPU_IDLE whenever nothing is `drawing`; the
    overload step cut to fit, without a glitch. `failures` names each failed check with its first
    moment's plan time."""
    passed: bool
    failures: tuple[str, ...]


def verdict(first: RegisterSample, moments: Sequence[Moment]) -> Verdict:
    """The verdict over `moments` (in order) against the run's `first` register sample. Pure."""
    raise NotImplementedError("render-planes S1 bead B6")
