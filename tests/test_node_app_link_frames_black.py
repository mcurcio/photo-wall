"""The Player's Frames black report on its probe channel: the wire shape and its refusals."""
import json

import pytest

from contracts.node_app_link import (
    MAX_FRAMES_BLACK,
    FrameBlack,
    FrameBlackCause,
    NodeFramesBlackMalformed,
    NodeFramesBlackV2,
    NodeProbeV2,
    encode_node_frames_black,
    encode_node_probe_answer,
    parse_node_probe_reply,
)

NONCE = "a" * 64
ONE = FrameBlack("HDMI-A-1", FrameBlackCause.NO_PLAN)


def test_a_report_round_trips_on_its_documented_wire():
    raw = encode_node_frames_black((ONE,))
    assert json.loads(raw) == {"schema": 2, "kind": "frames_black",
                               "frames": [{"output": "HDMI-A-1", "cause": "no_plan"}]}
    assert parse_node_probe_reply(raw) == NodeFramesBlackV2((ONE,))


def test_an_empty_report_is_a_report():
    assert parse_node_probe_reply(encode_node_frames_black(())) == NodeFramesBlackV2(())


@pytest.mark.parametrize("cause", list(FrameBlackCause))
def test_every_cause_crosses_the_wire(cause):
    report = (FrameBlack("HDMI-A-1", cause),)
    assert parse_node_probe_reply(encode_node_frames_black(report)).frames == report


def test_a_probe_answer_still_parses_as_one():
    assert parse_node_probe_reply(encode_node_probe_answer(NONCE)) == NodeProbeV2(NONCE)


@pytest.mark.parametrize("output_id", ["", "with space", "x" * 65, "nön-ascii", 7])
def test_an_output_name_past_the_rule_is_refused(output_id):
    with pytest.raises(ValueError, match="^probe_channel_message$"):
        FrameBlack(output_id, FrameBlackCause.NO_PLAN)


def test_a_cause_outside_the_vocabulary_is_refused():
    with pytest.raises(ValueError, match="^probe_channel_message$"):
        FrameBlack("HDMI-A-1", "no_plan")


@pytest.mark.parametrize("frames", [
    (FrameBlack("b", FrameBlackCause.NO_PLAN), FrameBlack("a", FrameBlackCause.NO_PLAN)),    # unsorted
    (FrameBlack("a", FrameBlackCause.NO_PLAN), FrameBlack("a", FrameBlackCause.NO_TIME)),    # twice
    tuple(FrameBlack(f"o{i:02}", FrameBlackCause.NO_PLAN) for i in range(MAX_FRAMES_BLACK + 1)),
])
def test_an_incoherent_report_is_refused(frames):
    with pytest.raises(ValueError, match="^probe_channel_message$"):
        encode_node_frames_black(frames)


@pytest.mark.parametrize("packet", [
    {"schema": 2, "kind": "frames_black"},                  # no frames
    {"schema": 2, "kind": "frames_black", "frames": "no"},
    {"schema": 2, "kind": "frames_black", "frames": [{"output": "a"}]},
    {"schema": 2, "kind": "frames_black", "frames": [{"output": "a", "cause": "a_future_one"}]},
    {"schema": 2, "kind": "frames_black", "frames": [{"output": "a", "cause": "no_plan", "more": 1}]},
    {"schema": 2, "kind": "frames_black", "frames": [{"output": "", "cause": "no_plan"}]},
])
def test_a_malformed_report_is_its_own_refusal_so_the_channel_survives_it(packet):
    with pytest.raises(NodeFramesBlackMalformed):
        parse_node_probe_reply(json.dumps(packet).encode())
