"""Canonical trial envelope and hash; base transports, Central/Player validate Calibration.

No geometry or homography implementation belongs in the base contract. Calibration
JSON is normalized by contracts.models.Calibration at its applying/authoring owner.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from contracts.node_display import Surface, document, surface_from
from contracts.node_protocol import counter, digest, identifier
from contracts.strict_json import loads_object

MAX_TRIAL_BYTES = 4096


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class TrialCandidate:
    trial_id: UUID
    generation: int
    sequence: int
    baseline: Surface
    calibration_json: str
    candidate_sha256: str
    expires_boottime_ms: int
    hard_expires_boottime_ms: int

    def __post_init__(self) -> None:
        identifier(self.trial_id)
        counter(self.generation, 1)
        counter(self.sequence, 1)
        counter(self.expires_boottime_ms, 1)
        counter(self.hard_expires_boottime_ms, 1)
        digest(self.candidate_sha256)
        if (
            type(self.baseline) is not Surface
            or self.expires_boottime_ms > self.hard_expires_boottime_ms
        ):
            raise ValueError("trial_scope_or_deadline")
        value = loads_object(self.calibration_json.encode(), max_bytes=2048)
        if value is None or canonical(value) != self.calibration_json:
            raise ValueError("trial_calibration_not_canonical")
        if self.candidate_sha256 != candidate_hash(
            self.trial_id, self.generation, self.sequence, self.baseline, self.calibration_json
        ):
            raise ValueError("trial_candidate_hash_mismatch")

    @property
    def frame_tag(self) -> str:
        return "trial-" + self.candidate_sha256


def candidate_hash(
    trial_id: UUID, generation: int, sequence: int, baseline: Surface, calibration_json: str
) -> str:
    return hashlib.sha256(
        canonical(
            {
                "trial_id": str(trial_id),
                "generation": generation,
                "sequence": sequence,
                "baseline": document(baseline),
                "calibration": json.loads(calibration_json),
            }
        ).encode()
    ).hexdigest()


def encode_trial(candidate: TrialCandidate) -> str:
    result = canonical(document(candidate))
    if len(result.encode()) > MAX_TRIAL_BYTES:
        raise ValueError("trial_envelope_bound")
    return result


def parse_trial(raw: str) -> TrialCandidate:
    value = loads_object(raw.encode(), max_bytes=MAX_TRIAL_BYTES)
    if value is None:
        raise ValueError("trial_envelope_invalid")
    try:
        return TrialCandidate(
            **{
                **value,
                "trial_id": UUID(value["trial_id"]),
                "baseline": surface_from(value["baseline"]),
            }
        )
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("trial_envelope_invalid") from exc
