"""Applying owner for operational trial transforms; uses the canonical calibration path.

Position's trial also carries the Frame's draft lighting (decision 0028: its lights, mat depth and
paper, `lighting_json` parsed as contracts.frame_lighting.FrameLighting): drawn while the trial is,
the binding's again once it ends. A draft FrameLighting refuses is refused with the trial."""

from __future__ import annotations

from dataclasses import dataclass

from contracts.frame_lighting import FrameLighting
from contracts.models import Calibration
from contracts.node_calibration import TrialCandidate, parse_trial
from player.rendering import OutputComposition


@dataclass(frozen=True)
class AppliedTrial:
    candidate: TrialCandidate
    calibration: Calibration
    primitives: tuple[tuple[float, float], ...]
    lighting: FrameLighting


class TrialState:
    """Per-Output local terminal fence. A delayed envelope cannot revive expiry. The trial's
    calibration may carry a Frame fit (1c); its primitives are that fit's corners."""

    def __init__(self):
        self.scope = None
        self.generation = 0
        self.trial_id = None
        self.sequence = 0
        self.hard_cap = 0
        self.terminal = False
        self.active: AppliedTrial | None = None

    def apply(
        self, raw: str | None, composition: OutputComposition, now_ms: int
    ) -> AppliedTrial | None:
        scope = (
            composition.binding.output_id,
            composition.binding.generation,
            composition.binding.configuration_revision,
        )
        if self.scope != scope:
            self.scope = scope
            self.generation = self.sequence = self.hard_cap = 0
            self.trial_id = None
            self.terminal = False
            self.active = None
        if self.active and now_ms >= self.active.candidate.expires_boottime_ms:
            self.active, self.terminal = None, True
        if not raw:
            if self.trial_id is not None:
                self.terminal = True
            self.active = None
            return None
        candidate = parse_trial(raw)
        baseline = candidate.baseline
        if (
            baseline.output.output_id,
            baseline.binding_generation,
            baseline.config_revision,
        ) != scope:
            return self.active
        same = (candidate.trial_id, candidate.generation) == (self.trial_id, self.generation)
        if candidate.generation < self.generation or (same and self.terminal):
            return None
        if candidate.generation == self.generation and not same:
            return self.active
        if now_ms >= candidate.expires_boottime_ms:
            self.trial_id, self.generation = candidate.trial_id, candidate.generation
            self.terminal, self.active = True, None
            return None
        if same and (
            candidate.sequence < self.sequence or candidate.hard_expires_boottime_ms > self.hard_cap
        ):
            return self.active
        if (
            same
            and candidate.sequence == self.sequence
            and self.active
            and (candidate.candidate_sha256 != self.active.candidate.candidate_sha256)
        ):
            raise ValueError("trial_sequence_hash_changed")
        calibration = Calibration.model_validate_json(candidate.calibration_json)
        lighting = FrameLighting.model_validate_json(candidate.lighting_json)
        if calibration.revision != composition.binding.calibration.revision:
            raise ValueError("trial_calibration_baseline")
        # The overlay's primitives are the drawn fit's corners (the opening, both modes); the base
        # never recomputes them.
        points = tuple(
            tuple(max(0.0, min(1.0, n)) for n in corner) for corner in calibration.fit.corners
        )
        self.trial_id, self.generation, self.sequence = (
            candidate.trial_id,
            candidate.generation,
            candidate.sequence,
        )
        self.hard_cap, self.terminal = candidate.hard_expires_boottime_ms, False
        self.active = AppliedTrial(candidate, calibration, points, lighting)
        return self.active
