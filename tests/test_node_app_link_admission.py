"""A Player's app link is admitted against the receipt its own state polls keep current."""
import json
from dataclasses import replace
from uuid import uuid4

from fastapi.testclient import TestClient
from test_node_runtime_reconciliation import rig
from test_registry import ADMIN

from central.app import create_app
from central.fleet.node_app_links import NodeAppLinks
from contracts.node_app_link import NodeAppLinkV2, encode_node_app_link, node_app_link_message
from contracts.player_control import ControlAck


def test_a_state_poll_between_acknowledgement_and_link_keeps_the_receipt_current(registry):
    """The Player polls state every half second while the broker delivers its link later, so
    a poll of unchanged state lands between the ACK and the link. That poll is the same
    delivery, the ACK replays the same receipt, and Central records the link. Mutation probe:
    issue a new delivery on every poll (the old rotation) and the link is refused with 409
    node_link_control_not_current."""
    _, player, key, sessions, claims, _, _, _, proof = rig(registry)
    headers = {"Authorization": "Bearer " + player["token"]}
    with TestClient(create_app(registry.db, registry.clock, ADMIN)) as client:
        state = client.get("/v1/player/state", headers=headers).json()
        report = {"authority_epoch": player["authority_epoch"],
                  "delivery_id": state["delivery_id"], "result": "applied"}
        applied = client.post("/v1/player/control-acks", json=report, headers=headers).json()
        assert applied["accepted"] is True
        # The Player's next poll and its ACK, before the broker's link reaches Central.
        polled = client.get("/v1/player/state", headers=headers).json()
        client.post("/v1/player/control-acks", headers=headers, json={
            **report, "delivery_id": polled["delivery_id"]})
    receipt = json.dumps(applied["receipt"], sort_keys=True, separators=(",", ":"))
    challenge = replace(proof.challenge, control_receipt=receipt, nonce=uuid4().hex * 2)
    link = NodeAppLinkV2(challenge, proof.public_key,
                         key.sign(node_app_link_message(challenge)).hex())
    broker = claims["app_effect_broker"]
    admitted = NodeAppLinks(sessions).admit(broker.session_id, broker.credential,
                                            encode_node_app_link(link))
    assert admitted["stored"] is True and admitted["duplicate"] is False


def _signed(key, proof, **changes):
    challenge = replace(proof.challenge, **changes)
    return encode_node_app_link(NodeAppLinkV2(challenge, proof.public_key,
                                              key.sign(node_app_link_message(challenge)).hex()))


def _row(registry, device_id):
    with registry.db.transaction() as conn:
        link = conn.execute("SELECT ctid::text AS ctid,xmin::text AS xmin,nonce,created_at,"
                            "created_by,changed_at,changed_by FROM node_app_links "
                            "WHERE device_id=%s", (device_id,)).fetchall()
        intake = conn.execute("SELECT coalesce(sum(used),0) AS n FROM node_intake_quotas "
                              "WHERE device_id=%s AND kind='evidence'", (device_id,)).fetchone()["n"]
    assert len(link) == 1  # one row per device generation: its current link
    return dict(link[0]), intake


def test_a_relink_that_restates_the_current_link_writes_nothing(registry):
    """The same process says again that it is linked, with a new nonce and a later sample (588
    of 658 relinks measured on 2026-11-15): the link row keeps its row version and no intake is
    counted; the answer is a 200 duplicate, current. A new control receipt is a new link: the
    row is replaced in place, `changed_*` stamped, `created_*` kept. Mutation probe: make
    `link_standing` compare the nonce (the relink rewrites the row)."""
    coordinator, player, key, sessions, claims, _, _, _, proof = rig(registry)
    broker = claims["app_effect_broker"]
    links = NodeAppLinks(sessions)
    device = proof.challenge.producer.device_id
    before, intake = _row(registry, device)
    assert (before["created_by"], before["changed_by"]) == ("node", "node")
    registry.clock.advance(5)
    again = links.admit(broker.session_id, broker.credential,
                        _signed(key, proof, nonce=uuid4().hex * 2, sampled_boottime_ms=900))
    assert again == {"stored": True, "duplicate": True, "current": True,
                     "admitted_at": before["changed_at"], "physical_identity": "unverified"}
    assert _row(registry, device) == (before, intake)
    # The exact retry, nonce included, restates too.
    assert links.admit(broker.session_id, broker.credential,
                       encode_node_app_link(proof))["duplicate"] is True
    assert _row(registry, device) == (before, intake)

    delivery = registry.issue_control_delivery_record(player["player_id"], 1, "f" * 64)
    receipt = registry.control_ack_response(player["player_id"], ControlAck(
        authority_epoch=1, delivery_id=delivery["delivery_id"], result="applied")).receipt
    fresh = json.dumps(receipt.model_dump(mode="json", by_alias=True), sort_keys=True,
                       separators=(",", ":"))
    replaced = links.admit(broker.session_id, broker.credential,
                           _signed(key, proof, nonce=uuid4().hex * 2, control_receipt=fresh))
    assert replaced["duplicate"] is False and replaced["current"] is True
    after, counted = _row(registry, device)
    assert counted == intake + 1
    assert after["xmin"] != before["xmin"]
    assert (after["created_at"], after["changed_at"]) == (before["created_at"],
                                                          registry.clock.utc())


def test_a_late_older_link_never_replaces_the_newer_one(registry):
    """Link A of a boot arrives after the newer link C of the same boot (a later app epoch):
    C's row stays as it is and A is answered 200, not stored, not current, so the Pi's outbox
    slot clears. Mutation probe: drop the `older` arm of `link_standing` (A replaces C)."""
    _, _, key, sessions, claims, _, _, _, proof = rig(registry)
    broker = claims["app_effect_broker"]
    links = NodeAppLinks(sessions)
    device = proof.challenge.producer.device_id
    newer = _signed(key, proof, nonce=uuid4().hex * 2, app_epoch=43, sampled_boottime_ms=700)
    assert links.admit(broker.session_id, broker.credential, newer)["duplicate"] is False
    current, intake = _row(registry, device)
    late = links.admit(broker.session_id, broker.credential,
                       _signed(key, proof, nonce=uuid4().hex * 2, sampled_boottime_ms=600))
    assert late == {"stored": False, "duplicate": False, "current": False, "admitted_at": None,
                    "physical_identity": "unverified"}
    assert _row(registry, device) == (current, intake)


def test_a_link_of_the_next_boot_replaces_the_current_one_whatever_its_counters(registry):
    """After a reboot the app's epoch and its boot-time sample start again, lower than the
    current link's: a link of another kernel boot is new, so it replaces the row (`changed_at`
    moves, one intake counted). Within one boot a link equal but for an earlier sample is a
    late retry: older, not a restatement, so it writes nothing. Mutation probes: drop the same
    boot test from `link_standing`'s `older` arm (the next boot is refused as older); drop the
    sample's `>=` from its `restates` arm (the earlier sample restates)."""
    from test_fleet_attempts import SERIAL
    from test_node_boot import boot_service, claim_for

    from contracts.node_boot import NodeBootRequestV2
    from contracts.node_protocol import NodeProcessIdentity

    _, _, key, sessions, claims, _, _, _, proof = rig(registry)
    broker = claims["app_effect_broker"]
    links = NodeAppLinks(sessions)
    device = proof.challenge.producer.device_id
    current, intake = _row(registry, device)
    earlier = links.admit(broker.session_id, broker.credential,
                          _signed(key, proof, nonce=uuid4().hex * 2, sampled_boottime_ms=400))
    assert earlier == {"stored": False, "duplicate": False, "current": False,
                       "admitted_at": None, "physical_identity": "unverified"}
    assert _row(registry, device) == (current, intake)

    registry.clock.advance(60)
    offer = boot_service(sessions).offer(NodeBootRequestV2(SERIAL, uuid4(), "b" * 64))
    claim = claim_for(offer, owner="app_effect_broker")
    grant = sessions.enroll(claim)
    assert grant.producer.kernel_boot_id != proof.challenge.producer.kernel_boot_id
    rebooted = _signed(key, proof, producer=grant.producer, command_session_id=grant.session_id,
                       process=NodeProcessIdentity(77, 1, uuid4()), app_epoch=1,
                       sampled_boottime_ms=10, nonce=uuid4().hex * 2)
    assert links.admit(grant.session_id, claim.credential, rebooted) == {
        "stored": True, "duplicate": False, "current": True,
        "admitted_at": registry.clock.utc(), "physical_identity": "unverified"}
    after, counted = _row(registry, device)
    assert after["xmin"] != current["xmin"] and counted == intake + 1
    assert (after["created_at"], after["changed_at"]) == (current["created_at"],
                                                          registry.clock.utc())
