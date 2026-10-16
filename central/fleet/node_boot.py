"""Exact V2 boot offers and immutable nonce decisions.

Deployments are written only by the release sync's ingest. Which one a Pi boots is its version
(a version per Pi, Q2 A, 2026-10-16: `PlayerVersions.boot_deployment_in`, its chosen version or
the newest ready stable release). The offer's wire is unchanged: a release's base carries the
release's own tag (scripts/node_release_artifacts.py), so `offer.base.tag` is the Pi's version.

Fleet chooses references; the shared content worker owns acquired bytes. Manager
roots are immutable properties of the selected base content, not a separate
mutable manager policy. No offer receipt proves download, boot, or playback.
"""
from __future__ import annotations

from typing import Final, Literal
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
from central.content_catalog.ports import PlayerVersions
from central.fleet.device_fence import fence_in, standing_in
from central.fleet.models import FleetError, OfferAsset
from central.fleet.node_sessions import NodeControlError, NodeSessions
from central.infra.transactions import PgTransaction
from contracts.node_boot import (
    NodeBootOfferV2,
    NodeBootRequestV2,
    encode_node_boot_offer,
    encode_node_boot_request,
    parse_node_boot_offer,
)

_DAY_SECONDS = 86400
_OFFER_DEVICE_DAILY = 128
_OFFER_GLOBAL_DAILY = 32768  # 128 Players x 128 offers, plus equivalent spoof headroom
_NEW_CLAIMS_GLOBAL_DAILY = 1024  # bounds distinct fake serial rows, above 128-Player ceiling
# The offer's policy revision (contracts/node_boot.py): a wall-wide selection's revision once; with a
# version per Pi there is no such revision, and the field stays on the wire at 1.
OFFER_POLICY_REVISION: Final = 1


def claim_quota(conn, *, device_id: str, kind: Literal["offer"], now: float) -> None:
    """Charge one unauthenticated boot offer to the day's global and per-device quotas, or
    refuse it with `t0_rate_limited` (429) when either is spent."""
    day = int(now // _DAY_SECONDS)
    limits = {"offer": (_OFFER_GLOBAL_DAILY, _OFFER_DEVICE_DAILY)}[kind]
    for scope, limit in zip(("global", device_id), limits, strict=True):
        row = conn.execute(
            "INSERT INTO fleet_t0_daily_quotas(scope,kind,day,used) "
            "VALUES(%s,%s,%s,1) ON CONFLICT(scope,kind,day) DO UPDATE SET "
            "used=fleet_t0_daily_quotas.used+1 WHERE fleet_t0_daily_quotas.used<%s "
            "RETURNING used", (scope, kind, day, limit),
        ).fetchone()
        if row is None:
            raise FleetError("t0_rate_limited", 429)
    # A bounded day index keeps fake-serial quota rows from accumulating forever.
    conn.execute("DELETE FROM fleet_t0_daily_quotas WHERE day<%s", (day - 30,))


def claim_new_device(conn, *, device_id: str, serial: str, now: float) -> None:
    """Create the device row for a serial seen for the first time, within the day's global
    new-device quota (`t0_claim_limit`, 429, once it is spent); a known device is a no-op."""
    if conn.execute("SELECT 1 FROM devices WHERE device_id=%s", (device_id,)).fetchone():
        return
    day = int(now // _DAY_SECONDS)
    row = conn.execute(
        "INSERT INTO fleet_t0_daily_quotas(scope,kind,day,used) "
        "VALUES('global','new_device',%s,1) ON CONFLICT(scope,kind,day) DO UPDATE SET "
        "used=fleet_t0_daily_quotas.used+1 "
        "WHERE fleet_t0_daily_quotas.used<%s RETURNING used",
        (day, _NEW_CLAIMS_GLOBAL_DAILY),
    ).fetchone()
    if row is None:
        raise FleetError("t0_claim_limit", 429)
    conn.execute("INSERT INTO devices(device_id,serial,first_seen,last_seen) "
                 "VALUES(%s,%s,%s,%s) ON CONFLICT(device_id) DO NOTHING",
                 (device_id, serial, now, now))


class NodeBootService:
    """The boot offer: each Pi's version (`versions`) chooses its deployment."""

    def __init__(self, sessions: NodeSessions, *, versions: PlayerVersions):
        self.sessions, self.versions = sessions, versions

    def offer(self, request: NodeBootRequestV2) -> NodeBootOfferV2:
        config = self.sessions.config
        serial = sanitize_serial(request.serial)
        device_id = device_id_for_serial(serial)
        if serial != request.serial or device_id is None:
            raise NodeControlError("node_serial_invalid", 422)
        canonical = encode_node_boot_request(request)
        refusal = None
        result = None
        with self.sessions.db.transaction() as conn:
            # The fence comes first, so an offer never waits on it holding the global quota row.
            fenced = fence_in(conn, device_id, "change")
            now = self.sessions.clock.utc()
            claim_quota(conn, device_id=device_id, kind="offer", now=now)
            claim_new_device(conn, device_id=device_id, serial=serial, now=now)
            generation = fenced.generation()
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
                chosen = self.versions.boot_deployment_in(PgTransaction(conn), serial)
                row = None if chosen is None else conn.execute(
                    "SELECT document FROM node_deployments WHERE deployment_id=%s", (chosen,)).fetchone()
                offer_id = uuid4()
                if row:
                    deployment = parse_node_deployment(bytes(row["document"]))
                    result = deployment.offer(offer_id=offer_id, audience=config.installation_audience,
                        request=request, device_id=device_id, generation=generation,
                        revision=OFFER_POLICY_REVISION, now=now)
                else:
                    refusal = "node_boot_no_ready_release"
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
        with self.sessions.db.read_snapshot() as conn:
            row = conn.execute("SELECT * FROM node_boot_offers WHERE offer_id=%s", (offer_id,)).fetchone()
            if row is None or row["offer_payload"] is None:
                raise NodeControlError("node_boot_offer_unknown", 404)
            offer = parse_node_boot_offer(bytes(row["offer_payload"]))
            generation = standing_in(conn, offer.device_id).active_generation()
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
