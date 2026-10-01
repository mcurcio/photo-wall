"""Opt-in browser fixture: real owner APIs, isolated DB, no command consumers."""
import os
import socket
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from test_fleet_attempts import BOOT_ID, DEVICE_ID, SERIAL
from test_fleet_rollout_gate import _certificate, _gate, _LocalImageVerifier
from test_node_boot import claim_for, cold_setup
from test_registry import enroll

from central.coordination import Coordinator
from central.fleet.node_routes import mount_node_routes
from central.fleet.node_sessions import NodeControlConfig
from central.media_repository import MediaRepository
from central.operator_snapshot import OperatorSnapshotReader
from central.runtime_store import RuntimeStore
from contracts.node_boot import NodeBootRequestV2


def test_operator_node_browser(registry):
    if os.environ.get("PHOTO_WALL_NODE_BROWSER") != "1":
        pytest.skip("interactive local fixture only")
    boots, sessions, _ = cold_setup(registry)
    boots.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64))
    second = boots.offer(NodeBootRequestV2(SERIAL, uuid4(), "b" * 64))
    # The later boot supersedes the earlier one; no operator selection exists.
    sessions.enroll(claim_for(second))
    enroll(registry, count=1, device_id=DEVICE_ID)
    coordinator = Coordinator(registry.db, registry.clock)
    gate, _ = _gate(registry, _certificate(expires_in=300))
    gate.open(expected_revision=0)
    app = FastAPI()
    mount_node_routes(app, db=registry.db, clock=registry.clock, admin=lambda: None,
        coordinator=coordinator, config=NodeControlConfig("node-test"),
        serving_verifier=_LocalImageVerifier())
    reader = OperatorSnapshotReader(registry.db, registry.clock, registry,
        RuntimeStore(registry.db, registry.clock), MediaRepository(registry.db, registry.clock), coordinator)
    done = threading.Event()
    seen = {"unknown": False}

    @app.middleware("http")
    async def disruptions(request, call_next):
        response = await call_next(request)
        if request.url.path.endswith("/reboots") and request.method == "POST" and response.status_code == 200 and not seen["unknown"]:
            # Simulate a gateway losing the successful response, preserving DB effect.
            seen["unknown"] = True
            return JSONResponse({"error": "fixture_response_lost"}, status_code=503)
        return response

    @app.get("/v1/operator/snapshot")
    def snapshot():
        return reader.read()

    @app.post("/fixture/done")
    def finish():
        done.set()
        return {"done": True}

    app.mount("/console", StaticFiles(directory=Path(__file__).resolve().parents[1] / "central/console/dist", html=True))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.01)
        print(f"NODE_BROWSER_URL http://127.0.0.1:{listener.getsockname()[1]}/console/#/equipment", flush=True)
        print(f"NODE_BROWSER_CURRENT_BOOT {second.kernel_boot_id}", flush=True)
        assert done.wait(330), "operator browser fixture timed out"
        with registry.db.transaction() as conn:
            commands = conn.execute("SELECT count(*) n FROM node_reboot_commands").fetchone()["n"]
        assert commands == 1 and all(seen.values())
        print("PASS browser_exact_unknown_reboot_retry", flush=True)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
