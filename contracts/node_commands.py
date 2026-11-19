"""The Node's owners and the producer document's reader (standard library).

Central's node sessions and the host's reboot record are gone (decision 0034, S4c-2a-v); a reboot
is the host line's `reboot` method (appliance/host/bus.py).
"""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from contracts.node_protocol import NodeProducerV2

Owner = Literal["host_core", "app_effect_broker", "display_host", "app_manager", "player_runtime"]


def producer_from_document(value: dict) -> NodeProducerV2:
    return NodeProducerV2(**{**value, "kernel_boot_id": UUID(value["kernel_boot_id"]),
                            "incarnation_id": UUID(value["incarnation_id"])})
