"""The tracer's verdict, pure (bead B6): each recorded moment held to what it must show."""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from scripts.render_bench.kms_state import OPAQUE_ALPHA, PREMULTIPLIED, KmsPlane
from scripts.render_bench.registers import RegisterSample, blind, glitched

ALPHA_TOLERANCE: Final = 1 / 255    # a picture's share of the screen, planes against strengths, within one level
GPU_IDLE: Final = 0.05              # the GPU's busy share that counts as idle


@dataclass(frozen=True)
class Moment:
    """One sample of the run: plan time `at`, the step's name, the strengths the rule gives
    (assignment id -> strength, timeline.py `strengths_at`), the kernel's planes then, the register
    sample, whether a picture was being finished or a video played since the moment before the
    last (`drawing`: a finish spills into the next interval, its fence signalled there), and the
    GPU's busy share since the last moment (v3d debugfs), the screen's mode as the controller
    scans it (`mode`, width by height) and whether the screen's ground must show beneath the
    pictures (`ground`, timeline.py `ground_seen`)."""
    at: float
    step: str
    expected: Mapping[str, float]
    planes: tuple[KmsPlane, ...]
    registers: RegisterSample
    drawing: bool
    gpu_busy: float
    mode: tuple[int, int] = (0, 0)
    ground: bool = False


@dataclass(frozen=True)
class Verdict:
    """The tracer's checks (s1-page.md, the tracer, and the fix rounds): no framebuffer of
    Weston's renderer on screen (the compositor composited nothing); the screen's ground on the
    bottom plane whenever it shows; every expected picture on its own plane above it; the planes' alphas mixing the screen
    the strengths do, each picture's share within ALPHA_TOLERANCE, and every blend mode Pre-multiplied; no glitch; the GPU below GPU_IDLE whenever nothing is
    `drawing`; the overload step cut to fit, without a glitch. `failures` names each failed check
    with its first moment's plan time."""
    passed: bool
    failures: tuple[str, ...]


def _ground(plane: KmsPlane, mode: tuple[int, int]) -> bool:
    """Whether `plane` is the screen's ground: the Player's opaque picture of the whole screen."""
    return plane.position == (0, 0) and plane.source_size == mode and plane.alpha == OPAQUE_ALPHA


def _shares(alphas: Sequence[float]) -> list[float]:
    """Each layer's share of the screen when `alphas` (bottom to top, 0..1) are stacked with
    pre-multiplied blending, each over everything beneath it: a layer's alpha times what every layer
    above it lets through. The rest is the ground's (or black's)."""
    shares, through = [], 1.0
    for alpha in reversed(alphas):
        shares.append(alpha * through)
        through *= 1.0 - alpha
    return shares[::-1]


def _mix_matches(alphas: Sequence[float], strengths: Sequence[float], slack: float) -> bool:
    """Whether planes at `alphas` (bottom to top) mix the picture `strengths` (compose's order, bottom
    to top) must: each picture's share of the screen within `slack`. The kernel's planes name no
    picture, so the shares are compared as sets, smallest with smallest (which pairs them as closely
    as any pairing can): a dissolve whose incoming picture is beneath at full strength and outgoing
    on top at 1 - s mixes the same screen as the plan's order, and passes; a share off by more than
    `slack` fails. The cost: a mix whose shares are right but swapped between its two pictures (the
    fade run backwards) passes too."""
    return all(abs(shown - wanted) <= slack
               for shown, wanted in zip(sorted(_shares(alphas)), sorted(_shares(strengths)), strict=True))


def _failed_checks(first: RegisterSample, moment: Moment, renderer: Collection[int]) -> list[str]:
    """The checks `moment` fails, in the verdict's order. A plane showing one of Weston's renderer
    framebuffers (`renderer`) is the compositor's work, never a picture; every other plane shows a
    client's buffer: bottom to top, the screen's ground when it shows (opaque over the whole
    screen), then one picture each, as the strengths are (`expected` keeps compose's order)."""
    failed = []
    pictures = [plane for plane in moment.planes if plane.framebuffer not in renderer]
    if len(pictures) != len(moment.planes):
        failed.append("composited")
    strengths = list(moment.expected.values())
    if moment.ground:
        if pictures and _ground(pictures[0], moment.mode):
            pictures = pictures[1:]
        else:
            failed.append("ground")
    if len(pictures) != len(strengths):
        failed.append("plane")
    slack = ALPHA_TOLERANCE + 0.5 / OPAQUE_ALPHA        # one level, and the property's own rounding
    if len(pictures) == len(strengths) and not _mix_matches(
            [plane.alpha / OPAQUE_ALPHA for plane in pictures], strengths, slack):
        failed.append("alpha")
    if any(plane.blend != PREMULTIPLIED for plane in pictures):
        failed.append("blend")
    if glitched(first, moment.registers):
        failed.append("glitch")
    if not moment.drawing and moment.gpu_busy > GPU_IDLE:
        failed.append("gpu")
    return failed


def verdict(first: RegisterSample, moments: Sequence[Moment], renderer: Collection[int] | None = None) -> Verdict:
    """The verdict over `moments` (in order) against the run's `first` register sample, `renderer`
    the framebuffer ids Weston's renderer drew into during the run (weston_log.py, from its
    drm-backend debug stream). Pure. A run that cannot show a failure fails too: no moments, no
    moment of the overload step, a `first` sample with every glitch bit already set
    (registers.py `blind`), or no renderer framebuffers known (None: no plane can be told from
    the compositor's own)."""
    failures: dict[str, float | None] = {}
    if blind(first):
        failures["glitch_blind"] = None
    if renderer is None:
        failures["renderer_unknown"] = None
        renderer = ()
    if not moments:
        failures["no_moments"] = None
    elif not any(moment.step == "overload" for moment in moments):
        failures["overload_unseen"] = None
    for moment in moments:
        for check in _failed_checks(first, moment, renderer):
            failures.setdefault(check, moment.at)
    named = tuple(check if at is None else f"{check} at {at:.3f}" for check, at in failures.items())
    return Verdict(not named, named)


__all__ = ["ALPHA_TOLERANCE", "GPU_IDLE", "Moment", "Verdict", "verdict"]
