"""Durable, default-closed admission for future fleet command effects.

This module cannot certify Kubernetes. A deployment-owned verifier must inspect
all routable paths, EndpointSlices, exact image digests, rollback bytes and
fence/readiness contracts under a deployment mutation lease. It supplies the
certificate itself; callers cannot open the gate with asserted digest strings.
The verifier must close the gate before changing a certified serving set and
must keep unsafe rollback images from becoming routable after a stop. No
production verifier is installed and this gate has no command/HTTP route.

Future effect writers must call ``require_open_in`` as their *first* database
lock and keep that transaction open through command commitment. The shared
row lock then serializes closure against in-flight admission on every pod.
"""

from __future__ import annotations

import json
import math
import re
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Protocol

from central.db import Database

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
MAX_CERTIFICATE_SECONDS = 300
MAX_CERTIFICATE_BYTES = 8192


class RolloutGateError(ValueError):
    """A closed, stale or uncertified gate never grants effect authority."""


@dataclass(frozen=True, slots=True)
class RolloutCertification:
    """Verifier result for one immutable serving and rollback scope.

    Values are evidence *references*, not self-authenticating proofs. Only the
    injected deployment verifier may construct this result for gate opening.
    Its contract is to check every direct and Gateway route, exact image bytes,
    the release compatibility matrix, rollback fencing and EndpointSlices.
    """

    deployment_uid: str
    deployment_generation: int
    serving_image_digests: tuple[str, ...]
    rollback_image_digests: tuple[str, ...]
    endpoint_slice_sha256: str
    route_inventory_sha256: str
    compatibility_matrix_sha256: str
    fence_contract_sha256: str
    readiness_contract_sha256: str
    evidence_ref: str
    verified_at: float
    expires_at: float


class RolloutVerifier(Protocol):
    """External adapter holds endpoint mutation away through context exit.

    Certification includes direct Service routes and the rollback artifact;
    the adapter must not rely on a Gateway-only view. The lease expiry is an
    additional bound, not a substitute for close-before-deployment-change.
    """

    def certify(self) -> AbstractContextManager[RolloutCertification]: ...


@dataclass(frozen=True, slots=True)
class MeasuredServingImage:
    """Trusted local measurement, never parsed from a command request."""

    deployment_uid: str
    image_digest: str
    measurement_ref: str
    rollout_scope_sha256: str | None = None
    verified_until: float | None = None


class ServingImageVerifier(Protocol):
    """Production adapter binds this process to its actual running image."""

    def measure(self) -> MeasuredServingImage: ...


@dataclass(frozen=True, slots=True)
class GateState:
    generation: int
    revision: int
    scope_sha256: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class GateAdmission(GateState):
    serving_image_digest: str
    rollback_image_digests: tuple[str, ...]
    backend_pid: int
    transaction_id: int
    measured_until: float | None = None

    def ensure_current_in(self, conn) -> None:
        """Recheck the lease immediately before committing a command effect.

        The caller still holds the shared gate row lock acquired by
        ``require_open_in``. A detached admission or another lock wait after
        first admission must not carry authority across closure or expiry.
        """
        transaction = _transaction_in(conn)
        if (transaction["backend_pid"] != self.backend_pid
                or transaction["transaction_id"] != self.transaction_id):
            raise RolloutGateError("rollout_admission_transaction_changed")
        row = conn.execute(
            "SELECT revision,generation,state,scope_sha256,expires_at "
            "FROM fleet_effect_gate WHERE singleton=TRUE FOR SHARE"
        ).fetchone()
        if (row is None or row["state"] != "open"
                or row["revision"] != self.revision
                or row["generation"] != self.generation
                or row["scope_sha256"] != self.scope_sha256
                or row["expires_at"] != self.expires_at):
            raise RolloutGateError("rollout_gate_closed")
        if _time_in(conn) >= min(self.expires_at, self.measured_until or self.expires_at):
            raise RolloutGateError("rollout_gate_closed")


def _time_in(conn) -> float:
    return float(conn.execute(
        "SELECT EXTRACT(EPOCH FROM clock_timestamp()) AS now"
    ).fetchone()["now"])


def _transaction_in(conn) -> dict[str, int]:
    return conn.execute(
        "SELECT pg_backend_pid() AS backend_pid, txid_current() AS transaction_id"
    ).fetchone()


def _scope(certificate: RolloutCertification) -> dict[str, object]:
    return {
        "deployment_uid": certificate.deployment_uid,
        "deployment_generation": certificate.deployment_generation,
        "serving_image_digests": certificate.serving_image_digests,
        "rollback_image_digests": certificate.rollback_image_digests,
        "endpoint_slice_sha256": certificate.endpoint_slice_sha256,
        "route_inventory_sha256": certificate.route_inventory_sha256,
        "compatibility_matrix_sha256": certificate.compatibility_matrix_sha256,
        "fence_contract_sha256": certificate.fence_contract_sha256,
        "readiness_contract_sha256": certificate.readiness_contract_sha256,
    }


def _validated_certificate(certificate: RolloutCertification, *, now: float
                           ) -> tuple[str, str]:
    if type(certificate) is not RolloutCertification:
        raise RolloutGateError("rollout_certificate_invalid")
    digests = certificate.serving_image_digests
    if (type(certificate.deployment_uid) is not str
            or not 1 <= len(certificate.deployment_uid) <= 256
            or type(certificate.deployment_generation) is not int
            or certificate.deployment_generation < 1
            or type(digests) is not tuple or not 1 <= len(digests) <= 128
            or any(type(item) is not str or _DIGEST.fullmatch(item) is None
                   for item in digests)
            or tuple(sorted(set(digests))) != digests
            or type(certificate.rollback_image_digests) is not tuple
            or not 1 <= len(certificate.rollback_image_digests) <= 128
            or any(type(item) is not str or _DIGEST.fullmatch(item) is None
                   for item in certificate.rollback_image_digests)
            or tuple(sorted(set(certificate.rollback_image_digests)))
            != certificate.rollback_image_digests):
        raise RolloutGateError("rollout_certificate_invalid")
    for digest in (certificate.endpoint_slice_sha256,
                   certificate.route_inventory_sha256,
                   certificate.compatibility_matrix_sha256,
                   certificate.fence_contract_sha256,
                   certificate.readiness_contract_sha256):
        if type(digest) is not str or _HEX.fullmatch(digest) is None:
            raise RolloutGateError("rollout_certificate_invalid")
    if (type(certificate.evidence_ref) is not str
            or not 1 <= len(certificate.evidence_ref) <= 512
            or type(certificate.verified_at) not in (int, float)
            or type(certificate.expires_at) not in (int, float)
            or not math.isfinite(certificate.verified_at)
            or not math.isfinite(certificate.expires_at)
            or certificate.verified_at > now + 5
            or certificate.expires_at <= now
            or certificate.expires_at - certificate.verified_at > MAX_CERTIFICATE_SECONDS
            or certificate.expires_at <= certificate.verified_at):
        raise RolloutGateError("rollout_certificate_stale")
    document = asdict(certificate)
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"),
                         allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_CERTIFICATE_BYTES:
        raise RolloutGateError("rollout_certificate_invalid")
    scope = json.dumps(_scope(certificate), sort_keys=True, separators=(",", ":"))
    return encoded, sha256(scope.encode("utf-8")).hexdigest()


def require_open_in(conn, *, expected_generation: int,
                    serving_verifier: ServingImageVerifier) -> GateAdmission:
    """Lock the current generation for the rest of the caller's transaction.

    Call before Coordination, Runtime or fleet locks. Expiry uses the database
    clock sampled after the lock wait. No caller-supplied time or digest can
    bypass a closed or expired row. Do not release the transaction before an
    effect is committed; otherwise closure can overtake it.
    """
    if type(expected_generation) is not int or expected_generation < 1:
        raise RolloutGateError("rollout_generation_invalid")
    if serving_verifier is None:
        raise RolloutGateError("rollout_serving_verifier_unavailable")
    identity = serving_verifier.measure()
    if (type(identity) is not MeasuredServingImage
            or type(identity.deployment_uid) is not str
            or not 1 <= len(identity.deployment_uid) <= 256
            or type(identity.image_digest) is not str
            or _DIGEST.fullmatch(identity.image_digest) is None
            or type(identity.measurement_ref) is not str
            or not 1 <= len(identity.measurement_ref) <= 256):
        raise RolloutGateError("rollout_serving_identity_invalid")
    row = conn.execute(
        "SELECT revision,generation,state,scope_sha256,certification,expires_at "
        "FROM fleet_effect_gate WHERE singleton=TRUE FOR SHARE"
    ).fetchone()
    if row is None:
        raise RolloutGateError("rollout_gate_missing")
    now = _time_in(conn)
    if identity.verified_until is not None and (type(identity.verified_until) not in (int, float)
            or not math.isfinite(identity.verified_until) or identity.verified_until <= now):
        raise RolloutGateError("rollout_measured_evidence_expired")
    if (row["state"] != "open" or row["generation"] != expected_generation
            or row["expires_at"] is None or float(row["expires_at"]) <= now):
        raise RolloutGateError("rollout_gate_closed")
    if (identity.rollout_scope_sha256 is not None
            and identity.rollout_scope_sha256 != row["scope_sha256"]):
        raise RolloutGateError("rollout_measured_topology_changed")
    certificate = row["certification"]
    if (type(certificate) is not dict
            or certificate.get("deployment_uid") != identity.deployment_uid
            or type(certificate.get("serving_image_digests")) is not list
            or identity.image_digest not in certificate["serving_image_digests"]
            or type(certificate.get("rollback_image_digests")) is not list
            or not certificate["rollback_image_digests"]
            or any(type(item) is not str or _DIGEST.fullmatch(item) is None
                   for item in certificate["rollback_image_digests"])):
        raise RolloutGateError("rollout_serving_scope_mismatch")
    try:
        certified_scope = {key: certificate[key] for key in _scope_keys()}
        encoded_scope = json.dumps(certified_scope, sort_keys=True, separators=(",", ":"))
    except (KeyError, TypeError, ValueError) as exc:
        raise RolloutGateError("rollout_serving_scope_mismatch") from exc
    if sha256(encoded_scope.encode("utf-8")).hexdigest() != row["scope_sha256"]:
        raise RolloutGateError("rollout_serving_scope_mismatch")
    transaction = _transaction_in(conn)
    return GateAdmission(row["generation"], row["revision"], row["scope_sha256"],
                         row["expires_at"], identity.image_digest,
                         tuple(certificate["rollback_image_digests"]),
                         transaction["backend_pid"], transaction["transaction_id"], identity.verified_until)


def _scope_keys() -> tuple[str, ...]:
    return (
        "deployment_uid", "deployment_generation", "serving_image_digests",
        "rollback_image_digests", "endpoint_slice_sha256", "route_inventory_sha256",
        "compatibility_matrix_sha256", "fence_contract_sha256",
        "readiness_contract_sha256",
    )


class RolloutEffectGate:
    """CAS transitions; no verifier means no opening or renewal path."""

    def __init__(self, db: Database, verifier: RolloutVerifier | None = None,
                 serving_verifier: ServingImageVerifier | None = None):
        self.db = db
        self._verifier = verifier
        self._serving_verifier = serving_verifier

    def require_open_in(self, conn, *, expected_generation: int) -> GateAdmission:
        """Use the trusted verifier configured at process composition."""
        if self._serving_verifier is None:
            raise RolloutGateError("rollout_serving_verifier_unavailable")
        return require_open_in(conn, expected_generation=expected_generation,
                               serving_verifier=self._serving_verifier)

    def status(self) -> dict[str, object]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT revision,generation,state,scope_sha256,expires_at,"
                "changed_at,reason FROM fleet_effect_gate WHERE singleton=TRUE"
            ).fetchone()
            if row is None:
                raise RolloutGateError("rollout_gate_missing")
            # An expired persisted 'open' row is never presented as authority.
            result = dict(row)
            result["effective_state"] = (
                "open" if row["state"] == "open"
                and row["expires_at"] is not None
                and float(row["expires_at"]) > _time_in(conn) else "closed"
            )
            return result

    def open(self, *, expected_revision: int) -> GateState:
        if type(expected_revision) is not int or expected_revision < 0:
            raise RolloutGateError("rollout_revision_invalid")
        if self._verifier is None:
            raise RolloutGateError("rollout_verifier_unavailable")
        # The external adapter owns the deployment lock until this context
        # exits, which must be after the database transaction commits.
        with self._verifier.certify() as certificate:
            with self.db.transaction() as conn:
                row = conn.execute(
                    "SELECT revision,generation,state FROM fleet_effect_gate "
                    "WHERE singleton=TRUE FOR UPDATE"
                ).fetchone()
                if row is None:
                    raise RolloutGateError("rollout_gate_missing")
                if row["revision"] != expected_revision:
                    raise RolloutGateError("rollout_revision_conflict")
                if row["state"] != "closed":
                    raise RolloutGateError("rollout_gate_already_open")
                # An unresolved equipment drain must be reconciled before a new effect
                # generation can start.
                if conn.execute("SELECT EXISTS(SELECT 1 FROM active_equipment_drains) AS drain"
                                ).fetchone()["drain"]:
                    raise RolloutGateError("rollout_barrier_unresolved")
                now = _time_in(conn)
                encoded, scope_digest = _validated_certificate(certificate, now=now)
                updated = conn.execute(
                    "UPDATE fleet_effect_gate SET revision=revision+1,"
                    "generation=generation+1,state='open',scope_sha256=%s,"
                    "certification=%s::jsonb,certified_at=%s,expires_at=%s,"
                    "changed_at=GREATEST(changed_at,%s),reason='deployment_certified' "
                    "WHERE singleton=TRUE RETURNING revision,generation,scope_sha256,expires_at",
                    (scope_digest, encoded, certificate.verified_at,
                     certificate.expires_at, now),
                ).fetchone()
                return GateState(**updated)

    def renew(self, *, expected_revision: int, expected_generation: int
              ) -> GateState:
        if (type(expected_revision) is not int or expected_revision < 1
                or type(expected_generation) is not int or expected_generation < 1):
            raise RolloutGateError("rollout_revision_invalid")
        if self._verifier is None:
            raise RolloutGateError("rollout_verifier_unavailable")
        with self._verifier.certify() as certificate:
            with self.db.transaction() as conn:
                row = conn.execute(
                    "SELECT revision,generation,state,scope_sha256,expires_at "
                    "FROM fleet_effect_gate WHERE singleton=TRUE FOR UPDATE"
                ).fetchone()
                if row is None:
                    raise RolloutGateError("rollout_gate_missing")
                if (row["revision"] != expected_revision
                        or row["generation"] != expected_generation):
                    raise RolloutGateError("rollout_revision_conflict")
                now = _time_in(conn)
                if row["state"] != "open" or row["expires_at"] <= now:
                    raise RolloutGateError("rollout_gate_closed")
                encoded, scope_digest = _validated_certificate(certificate, now=now)
                if (scope_digest != row["scope_sha256"]
                        or certificate.expires_at <= row["expires_at"]):
                    raise RolloutGateError("rollout_scope_changed")
                updated = conn.execute(
                    "UPDATE fleet_effect_gate SET revision=revision+1,"
                    "certification=%s::jsonb,certified_at=%s,expires_at=%s,"
                    "changed_at=GREATEST(changed_at,%s),reason='deployment_recertified' "
                    "WHERE singleton=TRUE RETURNING revision,generation,scope_sha256,expires_at",
                    (encoded, certificate.verified_at, certificate.expires_at, now),
                ).fetchone()
                return GateState(**updated)

    def close(self, *, expected_generation: int | None = None,
              reason: str = "deployment_changed") -> dict[str, object]:
        """Close without verifier; omitted generation is emergency closure."""
        if (expected_generation is not None
                and (type(expected_generation) is not int or expected_generation < 1)):
            raise RolloutGateError("rollout_generation_invalid")
        if type(reason) is not str or not 1 <= len(reason) <= 128:
            raise RolloutGateError("rollout_reason_invalid")
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT revision,generation,state FROM fleet_effect_gate "
                "WHERE singleton=TRUE FOR UPDATE"
            ).fetchone()
            if row is None:
                raise RolloutGateError("rollout_gate_missing")
            if expected_generation is not None and row["generation"] != expected_generation:
                raise RolloutGateError("rollout_generation_conflict")
            if row["state"] == "closed":
                return dict(row)
            now = _time_in(conn)
            return conn.execute(
                "UPDATE fleet_effect_gate SET revision=revision+1,state='closed',"
                "changed_at=GREATEST(changed_at,%s),reason=%s "
                "WHERE singleton=TRUE RETURNING revision,generation,state",
                (now, reason),
            ).fetchone()
