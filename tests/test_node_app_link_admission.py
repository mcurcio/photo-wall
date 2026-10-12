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
