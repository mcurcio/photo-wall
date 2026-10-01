#!/usr/bin/env python3
"""Sign/verify immutable exact-image CI evidence without changing release assets.

Private keys are explicit filesystem inputs from a separately provisioned CI
identity. No key generation, deployment mutation or positive unsigned fallback.
"""
from __future__ import annotations

import time
from hashlib import sha256
from pathlib import Path
from xml.etree import ElementTree

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from contracts.node_rollout import (
    QUALIFICATION_DOMAIN,
    REPORT_CATEGORIES,
    canonical,
    validate_qualification,
)
from contracts.strict_json import loads_object


def report_result(raw: bytes) -> dict:
    if len(raw) > 16*1024*1024 or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("qualification_report_invalid")
    root = ElementTree.fromstring(raw)
    cases = list(root.iter("testcase"))
    if not cases or any(list(case.iter("failure")) or list(case.iter("error")) or list(case.iter("skipped")) for case in cases):
        raise ValueError("qualification_report_not_all_passed")
    if any(int(suite.get(key, "0")) for suite in root.iter("testsuite") for key in ("failures", "errors", "skipped")):
        raise ValueError("qualification_report_not_all_passed")
    return {"sha256": sha256(raw).hexdigest(), "passed": len(cases)}


def verify_reports(payload, directory: Path):
    for image in payload["images"]:
        for category in REPORT_CATEGORIES:
            report = image["reports"][category]
            raw = (directory / (report["sha256"]+".xml")).read_bytes()
            if report_result(raw) != report:
                raise ValueError("qualification_report_mismatch")


def sign(payload: dict, key: Ed25519PrivateKey, reports: Path) -> bytes:
    validate_qualification(payload)
    verify_reports(payload, reports)
    return canonical({"payload": payload, "signature": key.sign(QUALIFICATION_DOMAIN+canonical(payload)).hex()})


def verify(raw: bytes, key: Ed25519PublicKey, reports: Path, *, now: float | None = None) -> dict:
    value = loads_object(raw, max_bytes=262144)
    if value is None or set(value) != {"payload", "signature"}:
        raise ValueError("qualification_signature_shape_invalid")
    payload = validate_qualification(value["payload"])
    key.verify(bytes.fromhex(value["signature"]), QUALIFICATION_DOMAIN+canonical(payload))
    now = time.time() if now is None else now
    if not payload["created_at"] <= now < payload["expires_at"]:
        raise ValueError("qualification_expired")
    verify_reports(payload, reports)
    return payload



class CiEvidencePublisher:
    """Concrete short-lived record issuer over verified immutable CI artifacts.

    The constructor is deployment configuration, never request-provided trust.
    A separately signed, fresh, monotonic revocation feed is mandatory. Observed
    revocation stays durable even when publication is refused or a feed omits it.
    This prepares exact ConfigMap bytes; its outbox does not mutate Kubernetes.
    """
    def __init__(self, db, *, audience, record_name, record_uid, key: Ed25519PrivateKey,
                 expected_suite_sha256, clock=time.time):
        self.db, self.audience, self.name, self.uid = db, audience, record_name, record_uid
        self.key, self.suite, self.clock = key, expected_suite_sha256, clock
        self.authority = sha256(key.public_key().public_bytes_raw()).hexdigest()

    def _revocations(self, raw):
        from contracts.node_rollout import REVOCATION_DOMAIN, document_hash
        value = loads_object(raw, max_bytes=131072)
        if value is None or set(value) != {"payload", "signature"}:
            raise ValueError("qualification_revocation_signature_required")
        p = value["payload"]
        expected = {"schema", "kind", "audience", "record_uid", "generation", "issued_at", "expires_at", "revoked_qualifications"}
        if type(p) is not dict or set(p) != expected or p["schema"] != 1 or p["kind"] != "qualification_revocations":
            raise ValueError("qualification_revocation_shape_invalid")
        self.key.public_key().verify(bytes.fromhex(value["signature"]), REVOCATION_DOMAIN+canonical(p))
        revoked = p["revoked_qualifications"]
        if (p["audience"] != self.audience or type(p["record_uid"]) is not str or not p["record_uid"]
                or type(p["generation"]) is not int or p["generation"] < 1 or type(revoked) is not list
                or len(revoked) > 1024 or any(type(h) is not str or len(h) != 64 or any(c not in "0123456789abcdef" for c in h) for h in revoked)
                or revoked != sorted(set(revoked))):
            raise ValueError("qualification_revocation_identity_invalid")
        # Commit signed revocation generations before freshness/state refusal.
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO node_ci_publication_heads(audience,record_name,record_uid,authority_sha256,"
                "revocation_uid,revocation_generation,revocation_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (self.audience, self.name, self.uid, self.authority, p["record_uid"], p["generation"], document_hash(p)))
            row = conn.execute("SELECT * FROM node_ci_publication_heads WHERE audience=%s AND record_name=%s FOR UPDATE",
                               (self.audience, self.name)).fetchone()
            if (row["record_uid"], row["authority_sha256"], row["revocation_uid"]) != (self.uid, self.authority, p["record_uid"]):
                raise ValueError("qualification_authority_reprovision_required")
            if p["generation"] < row["revocation_generation"] or (p["generation"] == row["revocation_generation"]
                    and document_hash(p) != row["revocation_sha256"]):
                raise ValueError("qualification_revocation_replay")
            conn.execute("UPDATE node_ci_publication_heads SET revocation_generation=%s,revocation_sha256=%s "
                "WHERE audience=%s AND record_name=%s", (p["generation"], document_hash(p), self.audience, self.name))
            for digest in revoked:
                conn.execute("INSERT INTO node_ci_revoked_qualifications VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                             (self.audience, self.name, digest))
        now = self.clock()
        if (any(type(p[k]) not in (int, float) for k in ("issued_at", "expires_at"))
                or not p["issued_at"] <= now < p["expires_at"] or p["expires_at"]-p["issued_at"] > 300):
            raise ValueError("qualification_revocation_feed_stale")
        return p

    def prepare(self, *, request_id, signed_qualification: bytes, reports: Path,
                signed_revocations: bytes, image_digests: tuple[str, ...]) -> bytes:
        from contracts.node_protocol import identifier
        from contracts.node_rollout import CI_LIVE_DOMAIN, document_hash, image_digest

        identifier(request_id)
        payload = verify(signed_qualification, self.key.public_key(), reports, now=self.clock())
        if payload["suite_sha256"] != self.suite:
            raise ValueError("qualification_suite_not_configured")
        qualified = tuple(sorted({image_digest(i["reference"]) for i in payload["images"]}))
        if qualified != image_digests:
            raise ValueError("qualification_scope_image_mismatch")
        revoked = self._revocations(signed_revocations)
        artifact_hash = document_hash(payload)
        input_hash = document_hash({"artifact": artifact_hash, "revocations": document_hash(revoked),
            "images": image_digests, "audience": self.audience, "record_uid": self.uid})
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM node_ci_publication_heads WHERE audience=%s AND record_name=%s FOR UPDATE",
                               (self.audience, self.name)).fetchone()
            if row["revocation_sha256"] != document_hash(revoked):
                raise ValueError("qualification_revocation_advanced")
            if conn.execute("SELECT 1 FROM node_ci_revoked_qualifications WHERE audience=%s AND record_name=%s "
                            "AND qualification_sha256=%s", (self.audience, self.name, artifact_hash)).fetchone():
                raise ValueError("qualification_revoked")
            prior = conn.execute("SELECT input_sha256,payload FROM node_ci_publications WHERE request_id=%s", (request_id,)).fetchone()
            if prior:
                if prior["input_sha256"] != input_hash:
                    raise ValueError("qualification_publication_identity_conflict")
                return bytes(prior["payload"])
            now = self.clock()
            expires = min(now+300, payload["expires_at"], revoked["expires_at"])
            if now >= expires:
                raise ValueError("qualification_evidence_expired_after_lock")
            generation = row["publication_generation"]+1
            live = {"audience": self.audience, "record_uid": self.uid, "generation": generation,
                    "state": "active", "issued_at": now, "expires_at": expires, "image_digests": list(image_digests)}
            for category in REPORT_CATEGORIES:
                live[category+"_sha256"] = document_hash({i["reference"]: i["reports"][category] for i in payload["images"]})
            raw = canonical({"payload": live, "signature": self.key.sign(CI_LIVE_DOMAIN+canonical(live)).hex()})
            conn.execute("UPDATE node_ci_publication_heads SET publication_generation=%s WHERE audience=%s AND record_name=%s",
                         (generation, self.audience, self.name))
            conn.execute("INSERT INTO node_ci_publications VALUES(%s,%s,%s,%s,%s,%s,%s)",
                         (request_id, self.audience, self.name, input_hash, generation, raw, expires))
            return raw
