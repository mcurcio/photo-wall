"""Canonical trial envelope and hash; base transports, Central/Player validate Calibration and FrameLighting.

A Position trial reaches the Pi on the Node bus's `display` line (decision 0034, S4b part 3): Central
puts a `TrialDocument` on each Output's `trial-<port>` desired key, the trial's scope (the binding
it is for: `TrialScope`) and the time left on each lease at the document's write as durations
(`idle_ms`, `hard_ms`), which the Display Host times from the document's arrival on its own boot
clock: neither side reads the other's clock. The Pi states on its `trial-<port>` state key what the
admitted surface presents under it (`TrialReport`, level state). The hash covers the scope, never
the whole admitted `Surface` (Central does not see the process), so Central, the Display Host and
the Player compute one `candidate_sha256`. On the Pi the trial becomes the envelope the shell and
the Player take (`TrialCandidate`: the admitted surface as `baseline`, deadlines on the Pi's boot
clock; `PositionTrial.on_pi`), and the shell still takes it only for the identity it holds.

No geometry or homography implementation belongs in the base contract. Calibration
JSON is normalized by contracts.models.Calibration at its applying/authoring owner.

Position's trial carries the Frame's lighting (contracts.frame_lighting.FrameLighting: its lights,
mat depth and paper) beside the calibration, hashed with it, so a presented trial proves both and
its Save writes both. On the wire the envelope's `lighting` is the lighting's JSON object. The
envelope's cap (MAX_TRIAL_BYTES) is the display host's C buffers', so a draft whose envelope would
pass it is refused before it is sent (`encode_trial`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from typing import Any, Final
from uuid import UUID

from pydantic import ValidationError

from contracts.frame_lighting import FrameLighting
from contracts.node_display import Surface, document, surface_from
from contracts.node_output import OUTPUT_IDS
from contracts.node_protocol import counter, digest, identifier, token
from contracts.strict_json import loads_object
from contracts.wire_codec import decode, encode, fields_of, require, whole

MAX_TRIAL_BYTES = 4096
MAX_CALIBRATION_BYTES = 2048
MAX_LIGHTING_BYTES = 1536                       # FRAME_LIGHTS_MAX lights at their longest fit
MAX_TRIAL_HARD_MS: Final = 120_000              # the hard lease's cap: native/shell.c refuses a trial past it
TRIAL_DOCUMENT_BYTES: Final = 5120              # MAX_TRIAL_BYTES plus the document's own fields
TRIAL_REPORT_BYTES: Final = 512
_TRIAL_PREFIX: Final = "trial-"                 # desired and state key: trial-<output_id>


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
    lighting: FrameLighting

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
        if type(self.lighting) is not FrameLighting or len(lighting_text(self.lighting).encode()) > MAX_LIGHTING_BYTES:
            raise ValueError("trial_lighting_invalid")
        if self.candidate_sha256 != candidate_hash(
            self.trial_id, self.generation, self.sequence, TrialScope.of(self.baseline),
            self.calibration_json, self.lighting,
        ):
            raise ValueError("trial_candidate_hash_mismatch")

    @property
    def frame_tag(self) -> str:
        return "trial-" + self.candidate_sha256


def lighting_text(lighting: FrameLighting) -> str:
    """The lighting's canonical JSON: what the envelope carries and the hash covers, at most
    MAX_LIGHTING_BYTES for a valid trial."""
    return canonical(lighting.model_dump(mode="json"))


@dataclass(frozen=True)
class TrialScope:
    """The binding a trial is for: what Central holds of the admitted surface (its Output, its Frame,
    the binding's generation and configuration revision; never the Player's process)."""
    output_id: str
    frame_id: str
    binding_generation: int
    config_revision: int

    def __post_init__(self) -> None:
        if self.output_id not in OUTPUT_IDS:
            raise ValueError("trial_scope")
        token(self.frame_id, 96)
        counter(self.binding_generation, 1)
        counter(self.config_revision, 1)

    @classmethod
    def of(cls, surface: Surface) -> TrialScope:
        """The scope of the admitted `surface`."""
        return cls(surface.output.output_id, surface.frame_id, surface.binding_generation,
                   surface.config_revision)


def candidate_hash(
    trial_id: UUID, generation: int, sequence: int, scope: TrialScope, calibration_json: str,
    lighting: FrameLighting,
) -> str:
    """The candidate's SHA-256 over its identity, scope, calibration and lighting (the trial's frame
    tag is "trial-" + it): Central computes it at each begin and edit, the Display Host checks it on
    receipt (`PositionTrial`) and the Player on its envelope (`TrialCandidate`)."""
    return hashlib.sha256(
        canonical(
            {
                "trial_id": str(trial_id),
                "generation": generation,
                "sequence": sequence,
                "scope": document(scope),
                "calibration": json.loads(calibration_json),
                "lighting": lighting.model_dump(mode="json"),
            }
        ).encode()
    ).hexdigest()


def encode_trial(candidate: TrialCandidate) -> str:
    """The envelope's canonical JSON; ValueError("trial_envelope_bound") past MAX_TRIAL_BYTES."""
    value = document(candidate)
    value["lighting"] = candidate.lighting.model_dump(mode="json")
    result = canonical(value)
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
                "lighting": FrameLighting.model_validate(value["lighting"]),
            }
        )
    except (KeyError, TypeError, AttributeError, ValidationError) as exc:
        raise ValueError("trial_envelope_invalid") from exc


_TRIAL_FIELDS = frozenset(field.name for field in fields(TrialCandidate))


@dataclass(frozen=True)
class PositionTrial:
    """A trial as Central sends it on an Output's trial document: its identity, scope, draft and
    lighting, its candidate's hash, and the time left on each lease at the document's write
    (`idle_ms`, `hard_ms`), timed on the Pi from the document's arrival."""
    trial_id: UUID
    generation: int
    sequence: int
    scope: TrialScope
    calibration_json: str
    lighting: FrameLighting
    candidate_sha256: str
    idle_ms: int
    hard_ms: int

    def __post_init__(self) -> None:
        identifier(self.trial_id)
        counter(self.generation, 1)
        counter(self.sequence, 1)
        digest(self.candidate_sha256)
        if (type(self.scope) is not TrialScope or not whole(self.idle_ms, 1)
                or not whole(self.hard_ms, self.idle_ms, MAX_TRIAL_HARD_MS)):
            raise ValueError("trial_scope_or_deadline")
        value = loads_object(self.calibration_json.encode(), max_bytes=MAX_CALIBRATION_BYTES)
        if value is None or canonical(value) != self.calibration_json:
            raise ValueError("trial_calibration_not_canonical")
        if type(self.lighting) is not FrameLighting or len(lighting_text(self.lighting).encode()) > MAX_LIGHTING_BYTES:
            raise ValueError("trial_lighting_invalid")
        if self.candidate_sha256 != candidate_hash(self.trial_id, self.generation, self.sequence, self.scope,
                                                   self.calibration_json, self.lighting):
            raise ValueError("trial_candidate_hash_mismatch")

    @property
    def frame_tag(self) -> str:
        return "trial-" + self.candidate_sha256

    def on_pi(self, baseline: Surface, received_ms: int) -> TrialCandidate:
        """The envelope the shell and the Player take: `baseline` the Output's admitted surface
        (ValueError("trial_not_current_admitted") unless its scope is this trial's), its deadlines
        `received_ms` (the Pi's boot clock when the document arrived) plus each lease's duration."""
        if type(baseline) is not Surface or TrialScope.of(baseline) != self.scope:
            raise ValueError("trial_not_current_admitted")
        return TrialCandidate(self.trial_id, self.generation, self.sequence, baseline, self.calibration_json,
                              self.candidate_sha256, received_ms + self.idle_ms, received_ms + self.hard_ms,
                              lighting=self.lighting)


@dataclass(frozen=True)
class TrialDocument:
    """What Central wants on one Output's trial key: the trial in force for the Frame bound there,
    or None. `change` rises only when the trial's identity, sequence, candidate or Central's
    deadlines differ (a keepalive moves the idle one), so a document put again never re-arms a
    lease on the Pi."""
    output_id: str
    change: int
    trial: PositionTrial | None

    def __post_init__(self) -> None:
        require(self.output_id in OUTPUT_IDS and whole(self.change, 1)
                and (self.trial is None or (type(self.trial) is PositionTrial
                                            and self.trial.scope.output_id == self.output_id)),
                "trial_document")


@dataclass(frozen=True)
class PresentedTrial:
    """The trial a Pi's admitted surface presents now: its identity, sequence and candidate."""
    trial_id: UUID
    generation: int
    sequence: int
    candidate_sha256: str

    def __post_init__(self) -> None:
        identifier(self.trial_id)
        counter(self.generation, 1)
        counter(self.sequence, 1)
        digest(self.candidate_sha256)


@dataclass(frozen=True)
class TrialReport:
    """What one Output presents under a trial now (level state): None when no trial is in force,
    its lease ran out, the admitted surface changed, or the trial is not presented yet."""
    output_id: str
    presented: PresentedTrial | None

    def __post_init__(self) -> None:
        require(self.output_id in OUTPUT_IDS
                and (self.presented is None or type(self.presented) is PresentedTrial), "trial_report")


def trial_document_key(output_id: str) -> str:
    """The desired-bucket key of `output_id`'s trial document: "trial-HDMI-A-1"; ValueError("output_id")."""
    if output_id not in OUTPUT_IDS:
        raise ValueError("output_id")
    return _TRIAL_PREFIX + output_id


def trial_report_key(output_id: str) -> str:
    """The state-bucket key of `output_id`'s trial report: "trial-HDMI-A-1"; ValueError("output_id")."""
    if output_id not in OUTPUT_IDS:
        raise ValueError("output_id")
    return _TRIAL_PREFIX + output_id


def _trial_wire(trial: PositionTrial) -> dict[str, Any]:
    value = document(trial)
    value["lighting"] = trial.lighting.model_dump(mode="json")
    return value


def encode_trial_document(document_: TrialDocument) -> bytes:
    """Canonical JSON of at most TRIAL_DOCUMENT_BYTES; ValueError("trial_document_too_large") past it."""
    return encode({"output_id": document_.output_id, "change": document_.change,
                   "trial": None if document_.trial is None else _trial_wire(document_.trial)},
                  TRIAL_DOCUMENT_BYTES, "trial_document_too_large")


def _position_trial(value: object) -> PositionTrial:
    value = fields_of(PositionTrial, value)
    scope = fields_of(TrialScope, value["scope"])
    if type(value["calibration_json"]) is not str or type(value["trial_id"]) is not str:
        raise ValueError("trial_document")
    try:
        lighting = FrameLighting.model_validate(value["lighting"])
    except ValidationError as error:
        raise ValueError("trial_document") from error
    return PositionTrial(**{**value, "trial_id": UUID(value["trial_id"]), "scope": TrialScope(**scope),
                            "lighting": lighting})


def decode_trial_document(raw: bytes) -> TrialDocument:
    """The document `raw` encodes (strict, exactly the dataclasses' field names); ValueError(
    "trial_document") for anything else."""
    def build(value: dict[str, Any]) -> TrialDocument:
        trial = value["trial"]
        return TrialDocument(**{**value, "trial": None if trial is None else _position_trial(trial)})
    return decode(raw, TRIAL_DOCUMENT_BYTES, TrialDocument, build, "trial_document")


def encode_trial_report(report: TrialReport) -> bytes:
    """Canonical JSON of at most TRIAL_REPORT_BYTES; ValueError("trial_report_too_large") past it."""
    return encode(document(report), TRIAL_REPORT_BYTES, "trial_report_too_large")


def decode_trial_report(raw: bytes) -> TrialReport:
    """The report `raw` encodes (strict); ValueError("trial_report") for anything else."""
    def build(value: dict[str, Any]) -> TrialReport:
        presented = value["presented"]
        if presented is not None:
            presented = fields_of(PresentedTrial, presented)
            if type(presented["trial_id"]) is not str:
                raise ValueError("trial_report")
            presented = PresentedTrial(**{**presented, "trial_id": UUID(presented["trial_id"])})
        return TrialReport(**{**value, "presented": presented})
    return decode(raw, TRIAL_REPORT_BYTES, TrialReport, build, "trial_report")
