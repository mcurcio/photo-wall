"""D17 gate is inert by default; certified openings need real PostgreSQL."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Event

import psycopg
import pytest

from central.fleet.rollout_gate import (
    MeasuredServingImage,
    RolloutCertification,
    RolloutEffectGate,
    RolloutGateError,
)

SERVING = "sha256:" + "a" * 64
ROLLBACK = "sha256:" + "b" * 64
OTHER = "sha256:" + "c" * 64
DEPLOYMENT = "photo-wall-installation-one"


def _certificate(*, expires_in: float = 60) -> RolloutCertification:
    now = time.time()
    return RolloutCertification(
        deployment_uid=DEPLOYMENT, deployment_generation=7,
        serving_image_digests=(SERVING,), rollback_image_digests=(ROLLBACK,),
        endpoint_slice_sha256="1" * 64, route_inventory_sha256="2" * 64,
        compatibility_matrix_sha256="3" * 64, fence_contract_sha256="4" * 64,
        readiness_contract_sha256="5" * 64,
        evidence_ref="deployment-controller/certification-7",
        verified_at=now, expires_at=now + expires_in,
    )


class _DeploymentVerifier:
    def __init__(self, certificate: RolloutCertification):
        self.certificate = certificate
        self.entered = 0
        self.exited = 0

    @contextmanager
    def certify(self):
        self.entered += 1
        try:
            yield self.certificate
        finally:
            self.exited += 1


class _LocalImageVerifier:
    def __init__(self, digest: str = SERVING, deployment: str = DEPLOYMENT):
        self.digest = digest
        self.deployment = deployment

    def measure(self):
        return MeasuredServingImage(
            deployment_uid=self.deployment, image_digest=self.digest,
            measurement_ref="trusted-pod-image-observation",
        )


def _gate(registry, certificate: RolloutCertification | None = None,
          *, local: _LocalImageVerifier | None = None):
    verifier = _DeploymentVerifier(certificate or _certificate())
    gate = RolloutEffectGate(registry.db, verifier, local or _LocalImageVerifier())
    return gate, verifier


def test_migration_is_closed_and_no_verifier_can_open(registry) -> None:
    gate = RolloutEffectGate(registry.db)
    assert gate.status()["effective_state"] == "closed"
    assert gate.status()["generation"] == 0
    assert gate.close()["state"] == "closed"
    with pytest.raises(RolloutGateError, match="rollout_verifier_unavailable"):
        gate.open(expected_revision=0)
    with registry.db.transaction() as conn:
        with pytest.raises(RolloutGateError, match="rollout_serving_verifier_unavailable"):
            gate.require_open_in(conn, expected_generation=1)


def test_certified_open_requires_exact_serving_image_and_generation(registry) -> None:
    gate, verifier = _gate(registry)
    opened = gate.open(expected_revision=0)
    assert (opened.generation, opened.revision) == (1, 1)
    assert verifier.entered == verifier.exited == 1
    assert gate.status()["effective_state"] == "open"
    with registry.db.transaction() as conn:
        admitted = gate.require_open_in(conn, expected_generation=opened.generation)
        assert admitted.serving_image_digest == SERVING
        assert admitted.rollback_image_digests == (ROLLBACK,)
        assert admitted.scope_sha256 == opened.scope_sha256
        with pytest.raises(RolloutGateError, match="rollout_gate_closed"):
            gate.require_open_in(conn, expected_generation=2)
    with pytest.raises(RolloutGateError, match="rollout_revision_conflict"):
        gate.open(expected_revision=0)
    with pytest.raises(RolloutGateError, match="rollout_gate_already_open"):
        gate.open(expected_revision=1)


@pytest.mark.parametrize("local", [
    _LocalImageVerifier(digest=OTHER),
    _LocalImageVerifier(deployment="different-installation"),
])
def test_local_identity_must_match_external_certification(registry, local) -> None:
    gate, _ = _gate(registry, local=local)
    gate.open(expected_revision=0)
    with registry.db.transaction() as conn:
        with pytest.raises(RolloutGateError, match="rollout_serving_scope_mismatch"):
            gate.require_open_in(conn, expected_generation=1)


def test_close_is_idempotent_revokes_and_reopen_uses_new_generation(registry) -> None:
    gate, verifier = _gate(registry)
    gate.open(expected_revision=0)
    with pytest.raises(RolloutGateError, match="rollout_generation_conflict"):
        gate.close(expected_generation=2)
    closed = gate.close(expected_generation=1, reason="endpoint_set_changing")
    assert (closed["revision"], closed["generation"], closed["state"]) == (2, 1, "closed")
    assert gate.close(expected_generation=1) == closed
    with registry.db.transaction() as conn:
        with pytest.raises(RolloutGateError, match="rollout_gate_closed"):
            gate.require_open_in(conn, expected_generation=1)
    verifier.certificate = replace(_certificate(), deployment_generation=8)
    reopened = gate.open(expected_revision=2)
    assert (reopened.revision, reopened.generation) == (3, 2)
    with registry.db.transaction() as conn:
        with pytest.raises(RolloutGateError, match="rollout_gate_closed"):
            gate.require_open_in(conn, expected_generation=1)


def test_expired_or_malformed_certification_and_scope_change_fail_closed(registry) -> None:
    gate, verifier = _gate(registry, _certificate(expires_in=-1))
    with pytest.raises(RolloutGateError, match="rollout_certificate_stale"):
        gate.open(expected_revision=0)
    assert gate.status()["effective_state"] == "closed"
    verifier.certificate = replace(_certificate(), serving_image_digests=(OTHER, SERVING))
    with pytest.raises(RolloutGateError, match="rollout_certificate_invalid"):
        gate.open(expected_revision=0)
    verifier.certificate = _certificate()
    gate.open(expected_revision=0)
    verifier.certificate = replace(_certificate(), endpoint_slice_sha256="f" * 64)
    with pytest.raises(RolloutGateError, match="rollout_scope_changed"):
        gate.renew(expected_revision=1, expected_generation=1)
    assert gate.status()["revision"] == 1


def test_renew_same_certified_scope_keeps_generation_and_extends_lease(registry) -> None:
    gate, verifier = _gate(registry, _certificate(expires_in=30))
    first = gate.open(expected_revision=0)
    verifier.certificate = replace(_certificate(expires_in=90),
                                   verified_at=verifier.certificate.verified_at + 1)
    renewed = gate.renew(expected_revision=1, expected_generation=1)
    assert renewed.revision == 2 and renewed.generation == 1
    assert renewed.expires_at > first.expires_at
    with pytest.raises(RolloutGateError, match="rollout_revision_conflict"):
        gate.renew(expected_revision=1, expected_generation=1)


def test_lease_expiry_is_checked_after_row_lock(registry, monkeypatch) -> None:
    gate, _ = _gate(registry)
    opened = gate.open(expected_revision=0)
    from central.fleet import rollout_gate as module

    actual_time = module._time_in
    monkeypatch.setattr(module, "_time_in", lambda conn: opened.expires_at + 1)
    with registry.db.transaction() as conn:
        with pytest.raises(RolloutGateError, match="rollout_gate_closed"):
            gate.require_open_in(conn, expected_generation=1)
    monkeypatch.setattr(module, "_time_in", actual_time)


def test_admission_rechecks_lease_after_downstream_wait(registry, monkeypatch) -> None:
    gate, _ = _gate(registry)
    opened = gate.open(expected_revision=0)
    from central.fleet import rollout_gate as module

    with registry.db.transaction() as conn:
        admission = gate.require_open_in(conn, expected_generation=1)
        monkeypatch.setattr(module, "_time_in", lambda _conn: opened.expires_at + 1)
        with pytest.raises(RolloutGateError, match="rollout_gate_closed"):
            admission.ensure_current_in(conn)


def test_admission_cannot_be_reused_in_another_transaction(registry) -> None:
    gate, _ = _gate(registry)
    gate.open(expected_revision=0)
    with registry.db.transaction() as conn:
        admission = gate.require_open_in(conn, expected_generation=1)
        admission.ensure_current_in(conn)
    with registry.db.transaction() as conn:
        with pytest.raises(RolloutGateError,
                           match="rollout_admission_transaction_changed"):
            admission.ensure_current_in(conn)


def test_closure_waits_for_transaction_scoped_admission(registry) -> None:
    gate, _ = _gate(registry)
    gate.open(expected_revision=0)
    close_started = Event()

    def close():
        close_started.set()
        return gate.close(expected_generation=1)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with registry.db.transaction() as conn:
            gate.require_open_in(conn, expected_generation=1)
            future = pool.submit(close)
            assert close_started.wait(timeout=3)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
        assert future.result(timeout=5)["state"] == "closed"
    with registry.db.transaction() as conn:
        with pytest.raises(RolloutGateError, match="rollout_gate_closed"):
            gate.require_open_in(conn, expected_generation=1)


def test_unresolved_drain_prevents_new_generation(registry) -> None:
    gate, _ = _gate(registry)
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO devices(device_id,first_seen,last_seen) VALUES(%s,1,1)",
                     ("device-" + "e" * 64,))
        conn.execute(
            "INSERT INTO players(id,public_key,token_hash,registered_at,last_seen,device_id) "
            "VALUES('p-drained','key','hash',1,1,%s)", ("device-" + "e" * 64,),
        )
        conn.execute(
            "INSERT INTO equipment_drains(player_id,attempt_id,boot_id,authority_epoch,"
            "phase,prepared_at,authorization_expires_at,snapshot) "
            "VALUES('p-drained','attempt','boot',1,'prepared',1,2,'{}'::jsonb)"
        )
    with pytest.raises(RolloutGateError, match="rollout_barrier_unresolved"):
        gate.open(expected_revision=0)
    assert gate.status()["effective_state"] == "closed"


def test_database_rejects_direct_nonmonotone_transition_and_delete(registry) -> None:
    with registry.db.transaction() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE fleet_effect_gate SET state='open' WHERE singleton=TRUE")
    with registry.db.transaction() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("DELETE FROM fleet_effect_gate WHERE singleton=TRUE")
