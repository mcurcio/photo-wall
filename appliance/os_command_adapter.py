"""Translate an admitted OS command receipt to unprivileged executor data.

The command inbox owns admission. This adapter adds no authority and performs
no app effect; it exists so the executor need not depend on the inbox type.
"""

from __future__ import annotations

from appliance.online_activation import OnlineArtifactRef, OnlineAttempt
from appliance.os_command import CommandReceipt
from contracts.os_command import ActivateAppCommand


def online_attempt_from_receipt(receipt: CommandReceipt) -> OnlineAttempt:
    if type(receipt) is not CommandReceipt or type(receipt.command) is not ActivateAppCommand:
        raise ValueError("online_receipt_invalid")
    command = receipt.command
    return OnlineAttempt(
        attempt_id=command.attempt_id,
        command_sha256=receipt.command_sha256,
        target=OnlineArtifactRef(command.target.sha256, command.target.size,
                                 command.target.base_abi),
        fallback=OnlineArtifactRef(command.fallback.sha256, command.fallback.size,
                                   command.fallback.base_abi),
    )
