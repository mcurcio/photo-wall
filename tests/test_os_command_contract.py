"""OS commands stay strict and bound to the base's verified session facts."""

import json
from dataclasses import replace
from uuid import uuid4

import pytest

from contracts.os_command import (
    LocalCommandContext,
    OsCommandError,
    parse_activate_app_command,
    require_current_command,
)
from contracts.player_payload import FORMAT

ABI = "sha256:" + "a" * 64


def command_document():
    return {
        "schema": 1, "kind": "activate_app", "installation_audience": "central.example:installation-1",
        "device_id": "device-" + "b" * 64, "device_generation": 3,
        "kernel_boot_id": str(uuid4()), "offer_id": str(uuid4()),
        "command_session_id": str(uuid4()), "attempt_id": str(uuid4()),
        "command_id": str(uuid4()), "desired_revision": 12, "drain_id": str(uuid4()),
        "target": {"format": FORMAT, "sha256": "c" * 64, "size": 1024, "base_abi": ABI},
        "fallback": {"format": FORMAT, "sha256": "d" * 64, "size": 1024, "base_abi": ABI},
        "expires_at": 1100.0,
    }


def parse(value):
    return parse_activate_app_command(json.dumps(value).encode())


def test_command_requires_exact_authenticated_context_and_short_session_deadline():
    command = parse(command_document())
    context = LocalCommandContext(
        command.installation_audience, command.device_id, command.device_generation,
        command.kernel_boot_id, command.offer_id, command.command_session_id, ABI,
        1000.0, 1200.0,
    )
    require_current_command(command, context)
    for changed in (
        replace(context, installation_audience="other-installation"),
        replace(context, device_generation=4),
        replace(context, kernel_boot_id=uuid4()),
        replace(context, offer_id=uuid4()),
        replace(context, command_session_id=uuid4()),
        replace(context, base_abi="sha256:" + "e" * 64),
        replace(context, now_utc=1100.0),
        replace(context, session_expires_at=1099.0),
        replace(context, now_utc=0.0, session_expires_at=1200.0),
    ):
        with pytest.raises(OsCommandError, match="os_command_context_mismatch"):
            require_current_command(command, changed)


@pytest.mark.parametrize("field,value", [
    ("schema", True), ("kind", "observe"), ("device_generation", True),
    ("device_id", "serial-only"), ("desired_revision", -1),
    ("command_id", "not-a-uuid"), ("expires_at", "1100"),
])
def test_command_rejects_malformed_identity_and_authority_fields(field, value):
    document = command_document()
    document[field] = value
    with pytest.raises(OsCommandError):
        parse(document)


def test_command_rejects_extra_fields_duplicate_keys_and_legacy_payload():
    document = command_document()
    document["bearer"] = "application-token"
    with pytest.raises(OsCommandError):
        parse(document)
    with pytest.raises(OsCommandError):
        parse_activate_app_command(b'{"schema":1,"schema":1}')
    document = command_document()
    document["target"]["format"] = "player-deb"
    with pytest.raises(OsCommandError):
        parse(document)


def test_command_refuses_same_digest_or_incompatible_fallback():
    document = command_document()
    document["fallback"]["sha256"] = document["target"]["sha256"]
    with pytest.raises(OsCommandError, match="os_command_artifact_conflict"):
        parse(document)
    document["fallback"]["sha256"] = "e" * 64
    document["fallback"]["base_abi"] = "sha256:" + "f" * 64
    with pytest.raises(OsCommandError, match="os_command_artifact_conflict"):
        parse(document)
