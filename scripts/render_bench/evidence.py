"""The tracer's verdict, pure (bead B6): each recorded moment held to what it must show."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from scripts.render_bench.kms_state import OPAQUE_ALPHA, PREMULTIPLIED, KmsPlane
from scripts.render_bench.registers import RegisterSample, blind, glitched

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


def _failed_checks(first: RegisterSample, moment: Moment) -> list[str]:
    """The checks `moment` fails, in the verdict's order. The screen's primary is its lowest plane
    (Weston's, holding the black); every plane above it is one picture, bottom to top as the
    strengths are (`expected` keeps compose's order)."""
    failed = []
    pictures = moment.planes[1:]
    strengths = list(moment.expected.values())
    if len(pictures) != len(strengths):
        failed.append("plane")
    slack = ALPHA_TOLERANCE + 0.5 / OPAQUE_ALPHA        # one level, and the property's own rounding
    if any(abs(plane.alpha / OPAQUE_ALPHA - strength) > slack
           for plane, strength in zip(pictures, strengths, strict=False)):
        failed.append("alpha")
    if any(plane.blend != PREMULTIPLIED for plane in pictures):
        failed.append("blend")
    if glitched(first, moment.registers):
        failed.append("glitch")
    if not moment.drawing and moment.gpu_busy > GPU_IDLE:
        failed.append("gpu")
    return failed


def verdict(first: RegisterSample, moments: Sequence[Moment]) -> Verdict:
    """The verdict over `moments` (in order) against the run's `first` register sample. Pure.
    A run that cannot show a failure fails too: no moments, no moment of the overload step, or a
    `first` sample with every glitch bit already set (registers.py `blind`)."""
    failures: dict[str, float | None] = {}
    if blind(first):
        failures["glitch_blind"] = None
    if not moments:
        failures["no_moments"] = None
    elif not any(moment.step == "overload" for moment in moments):
        failures["overload_unseen"] = None
    for moment in moments:
        for check in _failed_checks(first, moment):
            failures.setdefault(check, moment.at)
    named = tuple(check if at is None else f"{check} at {at:.3f}" for check, at in failures.items())
    return Verdict(not named, named)


__all__ = ["ALPHA_TOLERANCE", "GPU_IDLE", "Moment", "Verdict", "verdict"]
