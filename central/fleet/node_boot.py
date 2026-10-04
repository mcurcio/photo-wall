"""Exact V2 boot publications, selection, and immutable nonce decisions.

The deployment writer and the select writer live in `central/infra/node_releases.py`, shared
with the release sync (ingest, first-run selection); this service is their HTTP-facing half.

Fleet chooses references; the shared content worker owns acquired bytes. Manager
roots are immutable properties of the selected base content, not a separate
mutable manager policy. No offer receipt proves download, boot, or playback.
"""
from __future__ import annotations

from uuid import UUID, uuid4

from central.content_catalog.catalog import device_id_for_serial, sanitize_serial
from central.content_catalog.deployment import (  # re-exported: the model's historical home
    NodeDeployment as NodeDeployment,
)
from central.content_catalog.deployment import (
    encode_node_deployment as encode_node_deployment,
)
from central.content_catalog.deployment import (
    parse_node_deployment as parse_node_deployment,
)
from central.content_catalog.ports import NodeReleaseRefused
from central.fleet.models import OfferAsset
from central.fleet.node_sessions import NodeControlError, NodeSessions
from central.fleet.service import FleetService
from central.infra.asset_roots import lock_fleet_assets_in
from central.infra.node_releases import hand_provenance, select_deployment, write_deployment
from central.infra.transactions import PgTransaction
from central.kernel.job_types import Prefetch
from central.kernel.publishing import Publisher
from contracts.node_boot import (
    NodeBootOfferV2,
    NodeBootRequestV2,
    encode_node_boot_offer,
    encode_node_boot_request,
    parse_node_boot_offer,
)
from contracts.node_protocol import counter, identifier


def _refused(refused: NodeReleaseRefused) -> NodeControlError:
    return NodeControlError(refused.reason, 404 if refused.kind == "not_found" else 409)


class NodeBootService:
    """`publisher` warms the cache when a deployment is selected (a Prefetch in the selecting
    transaction, as catalog changes do); None where no content worker is wired."""

    def __init__(self, sessions: NodeSessions, *, publisher: Publisher | None = None):
        self.sessions, self.publisher = sessions, publisher

    def publish(self, deployment: NodeDeployment) -> dict:
        """The hand route (`POST /v1/operator/node/deployments`): a deployment written through
        the one deployment writer, its base and environments provided by an observed release
        (`node_release_catalog`). Releases themselves are ingested by the release sync."""
        self.sessions.require_enabled()
        try:
            with self.sessions.db.transaction() as conn:
                duplicate = write_deployment(conn, deployment,
                                             lambda: hand_provenance(conn, deployment),
                                             clock=self.sessions.clock)
        except NodeReleaseRefused as refused:
            raise _refused(refused) from None
        if duplicate:
            return {"published": True, "duplicate": True}
        return {"published": True, "duplicate": False, "bytes_verified": False}

    def select(self, deployment_id: UUID, expected_revision: int) -> dict:
        self.sessions.require_enabled()
        identifier(deployment_id)
        counter(expected_revision)
        try:
            with self.sessions.db.transaction() as conn:
                revision = select_deployment(conn, deployment_id, expected_revision,
                                             now=self.sessions.clock.utc())
                if self.publisher is not None:
                    self.publisher.publish(Prefetch(), within=PgTransaction(conn))
        except NodeReleaseRefused as refused:
            raise _refused(refused) from None
        return {"revision": revision, "deployment_id": str(deployment_id)}

    def offer(self, request: NodeBootRequestV2) -> NodeBootOfferV2:
        config = self.sessions.require_enabled()
        serial = sanitize_serial(request.serial)
        device_id = device_id_for_serial(serial)
        if serial != request.serial or device_id is None:
            raise NodeControlError("node_serial_invalid", 422)
        canonical = encode_node_boot_request(request)
        refusal = None
        result = None
        with self.sessions.db.transaction() as conn:
            lock_fleet_assets_in(conn)
            now = self.sessions.clock.utc()
            FleetService._claim_quota(conn, device_id=device_id, kind="offer", now=now)
            FleetService._claim_new_device(conn, device_id=device_id, serial=serial, now=now)
            generation = self.sessions.lock_device_generation_in(conn, device_id)
            prior = conn.execute("SELECT * FROM node_boot_offers WHERE device_id=%s AND "
                                 "(kernel_boot_id=%s OR boot_nonce=%s)",
                                 (device_id, request.kernel_boot_id, request.boot_nonce)).fetchone()
            if prior:
                if bytes(prior["request_payload"]) != canonical:
                    raise NodeControlError("node_boot_nonce_conflict")
                if prior["refusal"]:
                    refusal = prior["refusal"]
                elif prior["device_generation"] != generation:
                    raise NodeControlError("node_boot_generation_stale", 403)
                else:
                    result = parse_node_boot_offer(bytes(prior["offer_payload"]))
            else:
                policy = conn.execute("SELECT p.revision,d.document FROM node_boot_policy p "
                                      "JOIN node_deployments d USING(deployment_id) WHERE singleton").fetchone()
                offer_id = uuid4()
                if policy:
                    deployment = parse_node_deployment(bytes(policy["document"]))
                    result = deployment.offer(offer_id=offer_id, audience=config.installation_audience,
                        request=request, device_id=device_id, generation=generation,
                        revision=policy["revision"], now=now)
                else:
                    refusal = "node_boot_policy_unconfigured"
                conn.execute("INSERT INTO node_boot_offers(offer_id,device_id,device_generation,kernel_boot_id,"
                             "boot_nonce,request_payload,offer_payload,refusal,created_at) "
                             "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                             (offer_id, device_id, generation, request.kernel_boot_id, request.boot_nonce,
                              canonical, encode_node_boot_offer(result) if result else None, refusal, now))
                if result:
                    conn.execute("INSERT INTO node_offer_contexts(offer_id,basis,node_offer_id) VALUES(%s,'node_v2',%s)",
                                 (offer_id, offer_id))
        if refusal:
            raise NodeControlError(refusal, 503)
        assert result is not None
        return result

    def asset(self, offer_id: UUID, role: str) -> OfferAsset:
        self.sessions.require_enabled()
        with self.sessions.db.transaction() as conn:
            lock_fleet_assets_in(conn)
            row = conn.execute("SELECT * FROM node_boot_offers WHERE offer_id=%s", (offer_id,)).fetchone()
            if row is None or row["offer_payload"] is None:
                raise NodeControlError("node_boot_offer_unknown", 404)
            offer = parse_node_boot_offer(bytes(row["offer_payload"]))
            generation = self.sessions.lock_device_generation_in(conn, offer.device_id)
            if generation != offer.device_generation:
                raise NodeControlError("node_boot_generation_stale", 403)
            if role == "base":
                return OfferAsset("base", offer.base.tag, offer.base.content_key,
                                  offer.base.squashfs_sha256, offer.base.size_bytes)
            refs = {"app": offer.app_environment, "manager-primary": offer.manager_primary,
                    "manager-fallback": offer.manager_fallback}
            if role not in refs:
                raise NodeControlError("node_artifact_role_invalid", 404)
            ref = refs[role]
            if ref is None:
                raise NodeControlError("node_artifact_unconfigured", 404)
            return OfferAsset("environment", offer.base.tag, ref.environment_sha256,
                              ref.environment_sha256, ref.size_bytes, "sealed-environment-v2")
