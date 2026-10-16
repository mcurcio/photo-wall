"""Canonical trial envelope and hash; base transports, Central/Player validate Calibration and FrameLighting.

No geometry or homography implementation belongs in the base contract. Calibration
JSON is normalized by contracts.models.Calibration at its applying/authoring owner.

Room light (decision 0028) carries the Frame's lighting in Position's trial: `lighting_json` is the
canonical JSON of the draft's contracts.frame_lighting.FrameLighting (its lights, mat depth and
paper), carried and hashed beside the calibration, so a presented trial proves both and its Save
writes both; the console edits a Frame's lights live on the wall this way. The envelope's cap
(MAX_TRIAL_BYTES) is the display host's C buffers' (appliance/display_host/native), so a draft
whose envelope would pass it is refused before it is sent (`encode_trial`).

TRANSIT: until the central and pi lanes switch, `lighting_json` may still carry a print-look Mat's
JSON (NO_MAT_JSON); the central lane deletes NO_MAT_JSON.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from uuid import UUID

from contracts.node_display import Surface, document, surface_from
from contracts.node_protocol import counter, digest, identifier
from contracts.strict_json import loads_object

MAX_TRIAL_BYTES = 4096
MAX_CALIBRATION_BYTES = 2048
MAX_LIGHTING_BYTES = 1536                       # FRAME_LIGHTS_MAX lights at their longest fit
NO_MAT_JSON = '{"depth_mm":0.0,"lights":[]}'   # canonical(contracts.models.Mat().model_dump(mode="json"))


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
    lighting_json: str

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
        value = loads_object(self.calibration_json.encode(), max_bytes=MAX_CALIBRATION_BYTES)
        if value is None or canonical(value) != self.calibration_json:
            raise ValueError("trial_calibration_not_canonical")
        mat = (loads_object(self.lighting_json.encode(), max_bytes=MAX_LIGHTING_BYTES)
               if isinstance(self.lighting_json, str) else None)
        if mat is None or canonical(mat) != self.lighting_json:
            raise ValueError("trial_lighting_not_canonical")
        if self.candidate_sha256 != candidate_hash(
            self.trial_id, self.generation, self.sequence, self.baseline, self.calibration_json,
            self.lighting_json,
        ):
            raise ValueError("trial_candidate_hash_mismatch")

    @property
    def frame_tag(self) -> str:
        return "trial-" + self.candidate_sha256


def candidate_hash(
    trial_id: UUID, generation: int, sequence: int, baseline: Surface, calibration_json: str,
    lighting_json: str,
) -> str:
    """The candidate's SHA-256 over its identity, baseline, calibration and lighting (the trial's frame
    tag is "trial-" + it): Central computes it at each begin and edit, the Player checks it."""
    return hashlib.sha256(
        canonical(
            {
                "trial_id": str(trial_id),
                "generation": generation,
                "sequence": sequence,
                "baseline": document(baseline),
                "calibration": json.loads(calibration_json),
                "lighting": json.loads(lighting_json),
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
    if value is None or set(value) != _TRIAL_FIELDS:
        raise ValueError("trial_envelope_invalid")      # exactly the envelope's fields, none defaulted
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


_TRIAL_FIELDS = frozenset(field.name for field in fields(TrialCandidate))
