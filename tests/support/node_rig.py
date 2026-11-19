"""A bound Pi as Central knows it: one Player enrolled on the Pi's device with two Frames bound to
HDMI-A-1 and HDMI-A-2, a Run of black on both, the Player's control delivery applied, and the Pi's
boot offer. `surfaces` is the app's surface on each bound Output as Display Host would name it (no
Central route takes them since S4c part 2a-iv: a test reports Outputs on the display line,
tests/support/output_report.py)."""
from __future__ import annotations

from uuid import uuid4

from test_coordination import report
from test_fleet_attempts import BOOT_ID, DEVICE_ID, SERIAL
from test_registry import enroll, frame

from central.coordination import Coordinator
from central.runtime import Contribution, Scene
from contracts.node_protocol import NodeProcessIdentity, OutputKey, SurfaceFact
from contracts.player_control import ControlAck, ControlHello
from support.position import PORTRAIT_ON_WIDE, save_position


def rig(registry):
    """(coordinator, player, key, surfaces)."""
    from test_node_boot import cold_setup

    from contracts.node_boot import NodeBootRequestV2
    boots, _ = cold_setup(registry)
    boots.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64))
    player, key, _ = enroll(registry, device_id=DEVICE_ID)
    for index in range(2):
        frame(registry, f"node-f{index}")
        registry.bind(f"node-f{index}", player["player_id"], f"HDMI-A-{index+1}", expected_generation=0, by="console")
        save_position(registry, f"node-f{index}", PORTRAIT_ON_WIDE)
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.runtime.command("set_scene", Scene(scene_id="node-scene", loop=True, cycle_seconds=60,
        contributions=(Contribution(target="frame:node-f0", kind="black"),
                       Contribution(target="frame:node-f1", kind="black"),
                       Contribution(target="actuator:lamp", kind="actuator", ramp_to=1))), by="console")
    coordinator.runtime.command("activate", "node-scene", "node-run", 990, by="console")
    coordinator.advance()
    coordinator.readiness(player["player_id"], report(coordinator, player))
    registry.control_hello(player["player_id"], ControlHello(authority_epoch=1, schemas=(2,), capabilities=()))
    delivery = registry.issue_control_delivery_record(player["player_id"], 1, "c" * 64)
    registry.control_ack_response(player["player_id"], ControlAck(
        authority_epoch=1, delivery_id=delivery["delivery_id"], result="applied"))
    process, incarnation = NodeProcessIdentity(123, 10, uuid4()), uuid4()
    surfaces = [SurfaceFact(OutputKey(BOOT_ID, incarnation, binding.output_id, 1, 1),
                            process, 42, binding.generation, binding.configuration_revision,
                            "presented_to_compositor", f"buffer-{index}", frame_id=binding.frame_id)
                for index, binding in enumerate(coordinator.configuration(player["player_id"], 1).bindings)]
    return coordinator, player, key, surfaces
