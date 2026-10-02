"""Exact V2 boot publications, selection, and immutable nonce decisions.

Fleet chooses references; the shared content worker owns acquired bytes. Manager
roots are immutable properties of the selected base content, not a separate
mutable manager policy. No offer receipt proves download, boot, or playback.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from central.content_catalog.catalog import device_id_for_serial, sanitize_serial
from central.fleet.locks import lock_fleet_assets_in
from central.fleet.models import OfferAsset
from central.fleet.node_sessions import NodeControlError, NodeSessions
from central.fleet.service import FleetService
from central.infra.asset_records import PgAssetRecords
from central.infra.transactions import PgTransaction
from central.kernel.assets import AssetKey, AssetKind, AssetReference, OriginLocator
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_boot import (
    MAX_NODE_BOOT_BYTES,
    NodeBaseRefV2,
    NodeBootOfferV2,
    NodeBootRequestV2,
    encode_node_boot_offer,
    encode_node_boot_request,
    parse_node_boot_offer,
)
from contracts.node_protocol import counter, identifier
from contracts.node_release import parse_node_release
from contracts.strict_json import loads_object


@dataclass(frozen=True, slots=True)
class NodeDeployment:
    deployment_id: UUID
    base: NodeBaseRefV2
    app_environment: AppEnvironmentRefV2 | None
    manager_primary: AppEnvironmentRefV2
    manager_fallback: AppEnvironmentRefV2 | None
    environment_sources: dict[str, str]

    def offer(self, *, offer_id: UUID, audience: str, request: NodeBootRequestV2,
              device_id: str, generation: int, revision: int, now: float) -> NodeBootOfferV2:
        return NodeBootOfferV2(offer_id, audience, request.serial, device_id, generation,
            request.kernel_boot_id, request.boot_nonce, revision, uuid4(), int(now * 1000),
            int((now + 3600) * 1000), self.base,
            "selected" if self.app_environment is not None else "unconfigured", self.app_environment,
            self.manager_primary, self.manager_fallback)


def parse_node_deployment(raw: bytes) -> NodeDeployment:
    value = loads_object(raw, max_bytes=MAX_NODE_BOOT_BYTES)
    fields = {"deployment_id", "base", "app_environment", "manager_primary", "manager_fallback", "environment_sources"}
    if value is None or set(value) != fields or type(value["environment_sources"]) is not dict:
        raise ValueError("node_deployment_invalid")
    try:
        value["deployment_id"] = UUID(value["deployment_id"])
        value["base"] = NodeBaseRefV2(**value["base"])
        for name in ("app_environment", "manager_primary", "manager_fallback"):
            if value[name] is not None:
                value[name] = AppEnvironmentRefV2(**value[name])
        deployment = NodeDeployment(**value)
        # Reuse the canonical compatibility and bounds contract, never parallel rules.
        deployment.offer(offer_id=uuid4(), audience="validation", request=NodeBootRequestV2(
            "fixture", uuid4(), "0" * 64), device_id="device-" + "0" * 64,
            generation=1, revision=1, now=1)
        refs = [item for item in (deployment.app_environment, deployment.manager_primary,
                                 deployment.manager_fallback) if item is not None]
        if set(deployment.environment_sources) != {item.environment_sha256 for item in refs}:
            raise ValueError("node_environment_sources_mismatch")
        for item in refs:
            OriginLocator(deployment.environment_sources[item.environment_sha256],
                          item.environment_sha256, item.size_bytes)
        return deployment
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError("node_deployment_invalid") from exc


def encode_node_deployment(value: NodeDeployment) -> bytes:
    document = asdict(value)
    document["deployment_id"] = str(value.deployment_id)
    raw = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    parse_node_deployment(raw)
    return raw


class NodeBootService:
    def __init__(self, sessions: NodeSessions):
        self.sessions = sessions

    def publish(self, deployment: NodeDeployment) -> dict:
        self.sessions.require_enabled()
        canonical = encode_node_deployment(deployment)
        with self.sessions.db.transaction() as conn:
            lock_fleet_assets_in(conn)
            prior = conn.execute("SELECT document FROM node_deployments WHERE deployment_id=%s",
                                 (deployment.deployment_id,)).fetchone()
            if prior:
                if bytes(prior["document"]) != canonical:
                    raise NodeControlError("node_deployment_identity_conflict")
                return {"published": True, "duplicate": True}
            base = deployment.base
            manager_pins = {"base_abi": base.base_abi, "graphics_abi": base.graphics_abi,
                            "plugin_abi": base.plugin_abi,
                            "primary": asdict(deployment.manager_primary),
                            "fallback": asdict(deployment.manager_fallback) if deployment.manager_fallback else None}
            pins = conn.execute("SELECT document FROM node_base_managers WHERE base_content_key=%s",
                                (base.content_key,)).fetchone()
            if pins and pins["document"] != manager_pins:
                raise NodeControlError("node_base_manager_pins_immutable")
            publications = conn.execute("SELECT c.document,c.asset_locators FROM node_release_catalog c "
                "JOIN node_release_verifications v USING(manifest_sha256)").fetchall()
            release = None
            verified_environments = {}
            for publication in publications:
                manifest = parse_node_release(bytes(publication["document"]))
                locators = publication["asset_locators"]
                if manifest.base == base and manifest.manager_primary == deployment.manager_primary and manifest.manager_fallback == deployment.manager_fallback:
                    release = {"base_tarball_url": locators["base"]["url"], "base_tarball_size": locators["base"]["size"]}
                for role, ref in (("app", manifest.app_environment), ("manager-primary", manifest.manager_primary),
                                  ("manager-fallback", manifest.manager_fallback)):
                    if ref is not None:
                        verified_environments[(ref.environment_sha256, locators[role]["url"])] = ref
            if release is None:
                raise NodeControlError("node_base_release_provenance_unavailable", 409)
            for ref in (deployment.app_environment, deployment.manager_primary, deployment.manager_fallback):
                if ref is not None and verified_environments.get((ref.environment_sha256,
                        deployment.environment_sources[ref.environment_sha256])) != ref:
                    raise NodeControlError("node_environment_release_provenance_unavailable", 409)
            if not pins:
                conn.execute("INSERT INTO node_base_managers(base_content_key,document) VALUES(%s,%s)",
                             (base.content_key, Jsonb(manager_pins)))
            now = self.sessions.clock.utc()
            conn.execute("INSERT INTO node_deployments(deployment_id,document,published_at) VALUES(%s,%s,%s)",
                         (deployment.deployment_id, canonical, now))
            owner = "node-deployment:" + deployment.deployment_id.hex
            records, tx = PgAssetRecords(self.sessions.clock), PgTransaction(conn)
            references = [(AssetKey(AssetKind.OS_IMAGE, base.content_key),
                AssetReference(owner, OriginLocator(release["base_tarball_url"], base.content_key,
                    release["base_tarball_size"]), base.size_bytes, base.squashfs_sha256))]
            environments = {item.environment_sha256: item for item in (
                deployment.app_environment, deployment.manager_primary, deployment.manager_fallback) if item is not None}
            for item in environments.values():
                reference = asdict(item)
                old = conn.execute("SELECT reference FROM node_environment_catalog WHERE environment_sha256=%s",
                                   (item.environment_sha256,)).fetchone()
                if old and old["reference"] != reference:
                    raise NodeControlError("node_environment_identity_conflict")
                if old is None:
                    conn.execute("INSERT INTO node_environment_catalog(environment_sha256,reference) VALUES(%s,%s)",
                                 (item.environment_sha256, Jsonb(reference)))
                references.append((AssetKey(AssetKind.SEALED_ENVIRONMENT, item.environment_sha256),
                    AssetReference(owner, OriginLocator(deployment.environment_sources[item.environment_sha256],
                        item.environment_sha256, item.size_bytes), item.size_bytes, item.environment_sha256)))
            for key, reference in references:
                records.reference(tx, key, reference)
                conn.execute("INSERT INTO node_deployment_assets(deployment_id,kind,identity,owner) VALUES(%s,%s,%s,%s)",
                             (deployment.deployment_id, key.kind.value, key.identity, owner))
            return {"published": True, "duplicate": False, "bytes_verified": False}

    def select(self, deployment_id: UUID, expected_revision: int) -> dict:
        self.sessions.require_enabled()
        identifier(deployment_id)
        counter(expected_revision)
        with self.sessions.db.transaction() as conn:
            lock_fleet_assets_in(conn)
            row = conn.execute("SELECT revision FROM node_boot_policy WHERE singleton FOR UPDATE").fetchone()
            current = row["revision"] if row else 0
            if current != expected_revision:
                raise NodeControlError("node_boot_policy_conflict")
            if not conn.execute("SELECT 1 FROM node_deployments WHERE deployment_id=%s", (deployment_id,)).fetchone():
                raise NodeControlError("node_deployment_unknown", 404)
            conn.execute("INSERT INTO node_boot_policy(singleton,revision,deployment_id,changed_at) "
                         "VALUES(TRUE,%s,%s,%s) ON CONFLICT(singleton) DO UPDATE SET revision=EXCLUDED.revision,"
                         "deployment_id=EXCLUDED.deployment_id,changed_at=EXCLUDED.changed_at",
                         (current + 1, deployment_id, self.sessions.clock.utc()))
            return {"revision": current + 1, "deployment_id": str(deployment_id)}

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
