"""Post-ACK node process proof over the root-owned base broker socket."""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

from contracts.node_app_link import (
    MAX_NODE_LINK_BYTES,
    NodeAppLinkV2,
    encode_node_app_link,
    encode_node_app_link_begin,
    parse_node_app_link_challenge,
    parse_node_app_link_result,
)
from contracts.player_control import ControlAppliedReceipt
from player.identity import Identity
from player.local_app_proof import LocalProofError, _receive, _root_socket, _send

DEFAULT_NODE_LINK_SOCKET = Path("/run/photo-wall-client/app-link.sock")


class NodeAppLinkClient:
    def __init__(self, path: Path = DEFAULT_NODE_LINK_SOCKET, *, connector=_root_socket):
        self.path, self.connector = path, connector

    def exchange_applied(self, *, identity: Identity, player_id: str, authority_epoch: int,
                         device_id: str, kernel_boot_id: str, receipt: ControlAppliedReceipt,
                         enrollment_current: Callable[[], bool]) -> str:
        if not enrollment_current():
            return "stale"
        receipt_json = json.dumps(receipt.model_dump(mode="json", by_alias=True),
                                  sort_keys=True, separators=(",", ":"))
        with self.connector(self.path) as connection:
            connection.settimeout(2.0)
            _send(connection, json.loads(encode_node_app_link_begin(player_id=player_id,
                  authority_epoch=authority_epoch, control_receipt=receipt_json)), limit=MAX_NODE_LINK_BYTES)
            packet = _receive(connection, limit=MAX_NODE_LINK_BYTES)
            if packet.get("kind") == "error":
                return "rejected"
            challenge = parse_node_app_link_challenge(json.dumps(packet).encode())
            if (challenge.player_id != player_id or challenge.authority_epoch != authority_epoch
                    or challenge.control_receipt != receipt_json
                    or challenge.producer.device_id != device_id
                    or str(challenge.producer.kernel_boot_id) != kernel_boot_id
                    or challenge.process.pid != os.getpid()):
                raise LocalProofError("node_link_context_mismatch")
            if not enrollment_current():
                return "stale"
            proof: NodeAppLinkV2 = identity.sign_node_app_link(challenge)
            if not enrollment_current():
                return "stale"
            _send(connection, json.loads(encode_node_app_link(proof)), limit=MAX_NODE_LINK_BYTES)
            result = _receive(connection, limit=MAX_NODE_LINK_BYTES)
            try:
                status = parse_node_app_link_result(json.dumps(result).encode())
            except ValueError:
                return "rejected"
            # `accepted` (held by the broker) and `recorded` (by Central) both link this run.
            if status == "refused":
                return "rejected"
            return "recorded" if enrollment_current() else "stale"
