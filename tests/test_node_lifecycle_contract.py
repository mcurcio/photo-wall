from dataclasses import replace
from uuid import uuid4

import pytest
from test_node_boot import environment

from contracts.node_lifecycle import (
    AppEffectEventV2,
    StageCommandV2,
    StageReadyV2,
    StopPermitV2,
    encode_app_effect_event,
    encode_stage_command,
    encode_stage_ready,
    encode_stop_permit,
    parse_app_effect_event,
    parse_stage_command,
    parse_stage_ready,
    parse_stop_permit,
    ready_digest,
    stage_digest,
)
from contracts.node_protocol import NodeProcessIdentity, NodeProducerV2


def test_stage_ready_permit_effect_roundtrip_and_exact_digest():
    producer = NodeProducerV2("node-test", "device-" + "a" * 64, 1, uuid4(), "app_effect_broker", uuid4())
    process = NodeProcessIdentity(123, 456, uuid4())
    command = StageCommandV2(uuid4(), uuid4(), "0" * 64, producer, uuid4(), uuid4(),
                             process, 1, environment("a"), environment("b"), environment("a"), 100000)
    command = replace(command, command_sha256=stage_digest(command))
    assert parse_stage_command(encode_stage_command(command)) == command
    with pytest.raises(ValueError, match="digest_mismatch"):
        parse_stage_command(encode_stage_command(replace(command, expires_boottime_ms=99999)))
    ready = StageReadyV2(producer, command.operation_id, command.command_id, command.command_sha256,
        command.command_session_id, uuid4(), 1, 1000, process, 1,
        command.target.environment_sha256, command.fallback.environment_sha256, True, True)
    assert parse_stage_ready(encode_stage_ready(ready)) == ready
    permit = StopPermitV2(producer, command.operation_id, command.command_id, command.command_sha256,
        command.command_session_id, uuid4(), uuid4(), ready_digest(ready), process, 1, 1000, 31000)
    assert parse_stop_permit(encode_stop_permit(permit)) == permit
    with pytest.raises(ValueError, match="duration_invalid"):
        replace(permit, expires_boottime_ms=31001)
    event = AppEffectEventV2(producer, command.operation_id, command.command_id, command.command_sha256,
        command.command_session_id, uuid4(), 1, 1001, "intent_stop", permit.permit_id)
    assert parse_app_effect_event(encode_app_effect_event(event)) == event
    with pytest.raises(ValueError, match="process_required"):
        replace(event, phase="running")
    with pytest.raises(ValueError, match="permit_required"):
        replace(event, permit_id=None)
    with pytest.raises(ValueError, match="app_effect_producer_required"):
        replace(command, producer=replace(producer, owner="host_core"))
