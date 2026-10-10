"""The Player's app-link exchange: `accepted` and `recorded` both link the run; `refused` does not."""

import json
import os
import threading
from uuid import UUID

import pytest
from test_player_local_app_proof import _packet_pair

from contracts.node_app_link import (
    NodeAppLinkChallengeV2,
    encode_node_app_link_challenge,
    encode_node_app_link_result,
    parse_node_app_link,
)
from contracts.node_protocol import NodeProcessIdentity, NodeProducerV2
from contracts.player_control import ControlAppliedReceipt
from player.identity import load_identity
from player.node_app_link import NodeAppLinkClient

BOOT_ID = "12345678-1234-1234-1234-123456789abc"
DEVICE_ID = "device-" + "b" * 64
PLAYER_ID = "p-" + "c" * 32
RECEIPT = ControlAppliedReceipt(authority_epoch=7, delivery_id="e" * 32, delivery_sequence=5,
                                state_digest="f" * 64, ack_nonce="b" * 64)
RECEIPT_JSON = json.dumps(RECEIPT.model_dump(mode="json", by_alias=True),
                          sort_keys=True, separators=(",", ":"))


def _challenge() -> NodeAppLinkChallengeV2:
    producer = NodeProducerV2("site", DEVICE_ID, 1, UUID(BOOT_ID), "app_effect_broker", UUID(int=1))
    return NodeAppLinkChallengeV2(producer, UUID(int=2), NodeProcessIdentity(os.getpid(), 1, UUID(int=3)),
                                  1, "a" * 64, PLAYER_ID, 7, RECEIPT_JSON, "d" * 64, 1000)


def _exchange(result: bytes, *, current=lambda: True) -> str:
    app, broker = _packet_pair()
    app.settimeout(1)
    broker.settimeout(1)
    errors = []

    def serve():
        try:
            with broker:
                broker.recv(8193)
                broker.send(encode_node_app_link_challenge(_challenge()))
                parse_node_app_link(broker.recv(8193))
                broker.send(result)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        outcome = NodeAppLinkClient(connector=lambda _: app).exchange_applied(
            identity=load_identity(), player_id=PLAYER_ID, authority_epoch=7, device_id=DEVICE_ID,
            kernel_boot_id=BOOT_ID, receipt=RECEIPT, enrollment_current=current)
    finally:
        thread.join(timeout=3)
    assert not thread.is_alive() and not errors
    return outcome


@pytest.mark.parametrize("status", ["accepted", "recorded"])
def test_accepted_and_recorded_both_link_the_run(status):
    assert _exchange(encode_node_app_link_result(status)) == "recorded"


def test_refused_result_is_rejected():
    assert _exchange(encode_node_app_link_result("refused")) == "rejected"


@pytest.mark.parametrize("raw", [
    b'{"schema":2,"kind":"result","status":"maybe"}',
    b'{"schema":1,"kind":"result","status":"accepted"}',
    b'{"schema":2,"kind":"result","status":"accepted","extra":1}',
])
def test_any_other_result_is_rejected(raw):
    assert _exchange(raw) == "rejected"


def test_accepted_after_reenrollment_is_stale():
    calls = []

    def current():
        calls.append(1)
        return len(calls) < 4

    assert _exchange(encode_node_app_link_result("accepted"), current=current) == "stale"
