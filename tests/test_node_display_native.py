"""Opt-in real Linux compositor + production driver + mounted HTTP + PostgreSQL."""

import base64
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from test_fleet_attempts import DEVICE_ID, OFFER_ID, SERIAL
from test_registry import frame

from central.coordination import Coordinator
from central.fleet.node_app_links import NodeAppLinks
from central.fleet.node_routes import mount_node_routes
from central.fleet.node_sessions import NodeControlConfig, NodeSessions
from central.registry import Enrollment, OutputReport, enrollment_message
from contracts.node_app_link import (
    NodeAppLinkChallengeV2,
    NodeAppLinkV2,
    encode_node_app_link,
    node_app_link_message,
)
from contracts.node_commands import NodeSessionClaim
from contracts.node_protocol import NodeProcessIdentity
from contracts.player_control import ControlAck, ControlHello


def test_production_display_driver_native_chain(registry, monkeypatch):
    image = os.environ.get("PHOTO_WALL_NATIVE_DISPLAY_IMAGE")
    if not image:
        pytest.skip("set PHOTO_WALL_NATIVE_DISPLAY_IMAGE to pinned built Linux display image")
    app = FastAPI()
    coordinator = Coordinator(registry.db, registry.clock)
    config = NodeControlConfig("node-test")
    mount_node_routes(
        app,
        db=registry.db,
        clock=registry.clock,
        admin=lambda: None,
        coordinator=coordinator,
        config=config,
    )
    sessions = NodeSessions(registry.db, registry.clock, config)
    browser = os.environ.get("PHOTO_WALL_BROWSER_PROBE") == "1"
    browser_done = False
    fixture_player = None
    if browser:
        from fastapi.staticfiles import StaticFiles

        from central.media_repository import MediaRepository
        from central.operator_snapshot import OperatorSnapshotReader
        from central.runtime_store import RuntimeStore
        reader = OperatorSnapshotReader(registry.db, registry.clock, registry,
            RuntimeStore(registry.db, registry.clock), MediaRepository(registry.db, registry.clock), coordinator)

        @app.get("/v1/operator/snapshot")
        def snapshot():
            return reader.read()

        @app.post("/fixture/browser-done")
        def done():
            nonlocal browser_done
            browser_done = True
            return {"done": True}

        app.mount("/console", StaticFiles(directory=Path(__file__).resolve().parents[1] /
            "central/console/dist", html=True))


    @app.post("/fixture/ready")
    def ready(value: dict):
        nonlocal fixture_player
        import test_fleet_attempts

        monkeypatch.setattr(test_fleet_attempts, "BOOT_ID", UUID(value["boot_id"]))
        test_fleet_attempts._seed(registry)
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes_raw().hex()
        nonce = registry.challenge(public)["nonce"]
        outputs = (OutputReport(output_id="headless", width_px=640, height_px=480),)
        player = registry.enroll(
            Enrollment(
                public_key=public,
                nonce=nonce,
                outputs=outputs,
                device_id=DEVICE_ID,
                boot_id=value["boot_id"],
                ticket_id=None,
                signature=base64.b64encode(
                    key.sign(enrollment_message(nonce, outputs, DEVICE_ID, value["boot_id"], None))
                ).decode(),
            )
        )
        fixture_player = player["player_id"]
        frame(registry, "native-frame")
        registry.bind("native-frame", player["player_id"], "headless", expected_generation=0)
        registry.control_hello(
            player["player_id"], ControlHello(authority_epoch=1, schemas=(2,), capabilities=())
        )
        delivery = registry.issue_control_delivery_record(player["player_id"], 1, "c" * 64)
        receipt = registry.control_ack_response(
            player["player_id"],
            ControlAck(authority_epoch=1, delivery_id=delivery["delivery_id"], result="applied"),
        ).receipt
        claim = NodeSessionClaim(
            SERIAL,
            OFFER_ID,
            UUID(value["boot_id"]),
            "app_effect_broker",
            uuid4(),
            uuid4(),
            uuid4().hex + uuid4().hex,
        )
        grant = sessions.enroll(claim)
        process = NodeProcessIdentity(
            value["pid"], value["start_ticks"], UUID(value["invocation_id"])
        )
        challenge = NodeAppLinkChallengeV2(
            grant.producer,
            grant.session_id,
            process,
            1,
            "d" * 64,
            player["player_id"],
            1,
            json.dumps(
                receipt.model_dump(mode="json", by_alias=True),
                sort_keys=True,
                separators=(",", ":"),
            ),
            "e" * 64,
            value["boottime_ms"],
        )
        proof = NodeAppLinkV2(challenge, public, key.sign(node_app_link_message(challenge)).hex())
        NodeAppLinks(sessions).admit(
            grant.session_id, claim.credential, encode_node_app_link(proof)
        )
        with registry.db.transaction() as conn:
            configuration = registry.configuration_in(conn, player["player_id"], 1)
        return {"serial": SERIAL, "offer_id": str(OFFER_ID), "configuration": {
            "player_id": player["player_id"], "authority_epoch": 1, "configuration_revision": 1,
            "bindings": [b.model_dump(mode="json") for b in configuration["bindings"]],
            "enabled_outputs": [],
        }}

    @app.post("/fixture/advance")
    def advance():
        return {"advanced": True}  # The fixture clock advances with real compositor time.

    @app.post("/fixture/unbind")
    def unbind():
        registry.unbind("native-frame", expected_generation=1)
        return {"unbound": True}

    @app.get("/fixture/completed")
    def completed():
        with registry.db.transaction() as conn:
            configuration = registry.configuration_in(conn, fixture_player, 1)
            bindings = configuration["bindings"]
            return {
                "configuration": {"player_id": fixture_player, "authority_epoch": 1,
                    "configuration_revision": max((b.configuration_revision for b in bindings), default=1),
                    "bindings": [b.model_dump(mode="json") for b in bindings], "enabled_outputs": []},
                "browser_done": browser_done,
                "withdrawals": conn.execute("SELECT count(*) n FROM node_display_withdrawals").fetchone()["n"],
                "completed": conn.execute(
                    "SELECT count(*) n FROM node_display_handoffs"
                ).fetchone()["n"]
                >= 1
            }

    listener = socket.socket()
    listener.bind(("0.0.0.0", 0))
    listener.listen(8)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    ticking = threading.Event()
    def tick_clock():
        previous = time.monotonic()
        while not ticking.wait(0.1):
            current = time.monotonic()
            registry.clock.advance(current - previous)
            previous = current
    clock_thread = threading.Thread(target=tick_clock, daemon=True)
    clock_thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.01)
        if browser:
            print(f"BROWSER_URL http://127.0.0.1:{listener.getsockname()[1]}/console/#/wall/frames/native-frame/commissioning", flush=True)
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--platform",
                "linux/arm64",
                "-e",
                "PHOTO_WALL_GTK_PROBE=" + os.environ.get("PHOTO_WALL_GTK_PROBE", "0"),
                "-e", "PHOTO_WALL_BROWSER_PROBE=" + str(int(browser)),
                "-e", "PHOTO_WALL_CURRENT_PLAYER=" + os.environ.get("PHOTO_WALL_CURRENT_PLAYER", "0"),
                "-e", "PHOTO_WALL_BUILD_CURRENT_NATIVE=" + os.environ.get("PHOTO_WALL_BUILD_CURRENT_NATIVE", "0"),
                "-e", "PHOTO_WALL_MEDIA_PROBE=" + os.environ.get("PHOTO_WALL_MEDIA_PROBE", "0"),
                *(["--mount", f"type=bind,src={os.environ['PHOTO_WALL_DISPLAY_ARTIFACT_DIR']}/photo-wall-node-display_arm64.deb,dst=/node-display.deb,readonly",
                   "--mount", f"type=bind,src={os.environ['PHOTO_WALL_DISPLAY_ARTIFACT_DIR']}/libphoto-wall-frame-client.so,dst=/client.so,readonly"]
                  if os.environ.get("PHOTO_WALL_DISPLAY_ARTIFACT_DIR") else []),
                "--entrypoint",
                "/usr/bin/python3",
                "--mount",
                f"type=bind,src={os.environ.get('PHOTO_WALL_FROZEN_PLAYER', '/tmp/photo-wall-frozen-trial-player')},dst=/frozen-player,readonly",
                "--mount",
                f"type=bind,src={root},dst=/repo,readonly",
                image,
                "/repo/tests/native_display_service_probe.py",
                f"http://host.docker.internal:{listener.getsockname()[1]}",
            ],
            capture_output=True,
            text=True,
            timeout=360 if browser else 75,
        )
        if os.environ.get("PHOTO_WALL_NATIVE_EVIDENCE_LOG"):
            Path(os.environ["PHOTO_WALL_NATIVE_EVIDENCE_LOG"]).write_text(result.stdout + result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PASS production_driver_http_runtime_native_handoff" in result.stdout
        if not browser:
            assert "PASS production_driver_native_role_removal_before_response" in result.stdout
        if os.environ.get("PHOTO_WALL_MEDIA_PROBE") == "1":
            assert "PASS frozen_player_actual_media_assignment_witness" in result.stdout
        if os.environ.get("PHOTO_WALL_GTK_PROBE") == "1" and not browser:
            assert "PASS actual_gtk_partition_expiry_baseline_presentation" in result.stdout
        assert ("PASS interactive_operator_browser_fixture" if browser else
                "PASS first_calibration_operator_trial_native_save") in result.stdout
    finally:
        ticking.set()
        if browser:
            clock_thread.join(timeout=2)
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
