from dataclasses import replace
from uuid import uuid4

import pytest
from test_node_boot import environment

from contracts.node_lifecycle import (
    StageCommandV2,
    encode_stage_command,
    parse_stage_command,
    stage_digest,
)
from contracts.node_protocol import NodeProcessIdentity, NodeProducerV2


def test_stage_roundtrip_and_exact_digest():
    producer = NodeProducerV2("node-test", "device-" + "a" * 64, 1, uuid4(), "app_effect_broker", uuid4())
    process = NodeProcessIdentity(123, 456, uuid4())
    command = StageCommandV2(uuid4(), uuid4(), "0" * 64, producer, uuid4(), uuid4(),
                             process, 1, environment("a"), environment("b"), environment("c"))
    command = replace(command, command_sha256=stage_digest(command))
    assert parse_stage_command(encode_stage_command(command)) == command
    with pytest.raises(ValueError, match="digest_mismatch"):
        parse_stage_command(encode_stage_command(replace(command, old_app_epoch=2)))
    # A stage is executable only with a qualified fallback; absence is unrepresentable.
    with pytest.raises(ValueError, match="app_environment_required"):
        replace(command, fallback=None)
    with pytest.raises(ValueError, match="app_effect_producer_required"):
        replace(command, producer=replace(producer, owner="host_core"))
