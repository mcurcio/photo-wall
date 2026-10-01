from dataclasses import replace
from uuid import UUID

import pytest

from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_protocol import (
    AppProcessFact,
    NodeCommandResponseV2,
    NodeEventV2,
    NodeProcessIdentity,
    NodeProducerV2,
    NodeSnapshotV2,
    OutputKey,
    RebootFact,
    SurfaceFact,
    encode_node_message,
    parse_node_message,
)


def producer(owner="host_core"):
    return NodeProducerV2("test", "device-" + "a" * 64, 1, UUID(int=1), owner, UUID(int=2))


def environment():
    return AppEnvironmentRefV2("a" * 64, 100, "b" * 64, "photo-wall-player", "1:1.0~rc1",
                               "arm64", "c" * 64, "d" * 64, "/usr/bin/player",
                               "base-v2", "graphics-v1", "plugin-v1")


@pytest.mark.parametrize("message", [
    NodeEventV2(producer(), UUID(int=3), 1, 12, (RebootFact("unknown", "base_observer"),)),
    NodeSnapshotV2(producer(), UUID(int=4), 1, 13, (RebootFact("unknown", "base_observer"),)),
    NodeCommandResponseV2(producer(), UUID(int=5), "f" * 64, UUID(int=6),
                          "operator_reboot", "accepted", "accepted"),
])
def test_round_trip_independent_message_roles(message):
    assert parse_node_message(encode_node_message(message)) == message


@pytest.mark.parametrize("raw", [b'{}', b'{"schema":2,"schema":2,"message":{}}',
                                      b'{"schema":true,"message":{}}', b'x' * 65537,
                                      b'{"schema":2,"message":{"type":[]}}'])
def test_wire_rejects_malformed_or_unbounded_inputs(raw):
    with pytest.raises(ValueError):
        parse_node_message(raw)


@pytest.mark.parametrize("field,value", [("sequence", True), ("sequence", -1),
                                            ("occurred_boottime_ms", float("nan"))])
def test_event_rejects_coerced_or_nonfinite_counters(field, value):
    event = NodeEventV2(producer(), UUID(int=3), 1, 12, (RebootFact("unknown", "base_observer"),))
    with pytest.raises(ValueError):
        replace(event, **{field: value})


def test_fact_owner_and_scope_are_not_interchangeable():
    with pytest.raises(ValueError):
        NodeEventV2(producer("display_host"), UUID(int=3), 1, 12,
                    (RebootFact("unknown", "base_observer"),))
    with pytest.raises(ValueError):
        NodeCommandResponseV2(producer(), UUID(int=5), "f" * 64, UUID(int=6),
                              "app_effect", "accepted", "accepted")


def test_presentation_needs_exact_buffer_and_producer_incarnation():
    p = producer("display_host")
    output = OutputKey(p.kernel_boot_id, p.incarnation_id, "HDMI-A-1", 1, 1)
    process = NodeProcessIdentity(100, 10, UUID(int=7))
    with pytest.raises(ValueError):
        SurfaceFact(output, process, 1, 1, 1, "presented_to_compositor")
    fact = SurfaceFact(output, process, 1, 1, 1, "presented_to_compositor", "buffer-1")
    with pytest.raises(ValueError):
        NodeEventV2(replace(p, incarnation_id=UUID(int=8)), UUID(int=3), 1, 12, (fact,))


@pytest.mark.parametrize("field,value", [("entry_point", "/usr/../bin/player"),
    ("entry_point", "bin/player"), ("entry_point", "/usr//bin/player"),
    ("environment_sha256", "tag:latest"), ("size_bytes", True)])
def test_environment_ref_is_exact_and_entry_point_contained(field, value):
    with pytest.raises(ValueError):
        replace(environment(), **{field: value})


def test_process_fact_wire_round_trip():
    fact = AppProcessFact(NodeProcessIdentity(100, 10, UUID(int=7)), 1, "a" * 64, "running")
    event = NodeEventV2(producer("app_effect_broker"), UUID(int=3), 1, 12, (fact,))
    assert parse_node_message(encode_node_message(event)) == event
