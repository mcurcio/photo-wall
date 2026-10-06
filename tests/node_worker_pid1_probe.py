"""Container-only fixture for scripts.node_service_probe; never a physical effect proof."""
# ruff: noqa: E402 -- imports intentionally resolve the installed private base closures.
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, "/usr/lib/photo-wall-app-broker")
from appliance.boot_store import BootStore
from appliance.clock import boot_id, boottime_ms
from appliance.node.environment import verify_root
from appliance.node.import_worker import RootImportWorker
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_lifecycle import StageCommandV2, stage_digest
from contracts.node_protocol import NodeProcessIdentity, NodeProducerV2

ref = AppEnvironmentRefV2(**json.loads(Path("/var/lib/node-worker-app.json").read_text()))
producer = NodeProducerV2(
    "pid1-fixture", "device-" + "a" * 64, 1, boot_id(), "app_effect_broker", uuid4()
)
command = StageCommandV2(
    uuid4(),
    uuid4(),
    "0" * 64,
    producer,
    uuid4(),
    uuid4(),
    NodeProcessIdentity(98765, 1, uuid4()),
    1,
    replace(ref, environment_sha256="f" * 64),
    ref,
    None,
    boottime_ms() + 120000,
)
command = replace(command, command_sha256=stage_digest(command))
downloads = Path("/run/photo-wall-node-storage/preparation/downloads")
downloads.mkdir(mode=0o700, exist_ok=True)
os.chown(downloads, 10003, 10003)
archive = downloads / (ref.environment_sha256 + ".tar")
shutil.copyfile("/var/lib/node-worker-app.tar", archive)
os.chown(archive, 10003, 10003)
archive.chmod(0o600)
directory = Path("/run/photo-wall-app-broker")
directory.mkdir(mode=0o700, exist_ok=True)
store = BootStore(directory, boot_id=boot_id(), policy={"fixture": "worker"})
worker = RootImportWorker(store)
worker.advance(command)
first = store.read("import-worker")
assert first["identity"] is not None
unit = first["unit"]
print(
    subprocess.check_output(
        [
            "systemctl",
            "show",
            unit,
            "-p",
            "MainPID",
            "-p",
            "InvocationID",
            "-p",
            "ControlGroup",
            "-p",
            "MemoryMax",
            "-p",
            "CPUQuotaPerSecUSec",
            "-p",
            "MemoryCurrent",
            "-p",
            "TasksMax",
        ],
        text=True,
    ),
    flush=True,
)
store.close()
store = BootStore(directory, boot_id=boot_id(), policy={"fixture": "worker"})
worker = RootImportWorker(store)
worker.advance(command)
assert store.read("import-worker") == first
print(
    "PASS durable worker identity recovered during actual PID1 import without duplicate spawn",
    flush=True,
)
deadline = time.monotonic() + 120
while not worker.ready(command):
    if time.monotonic() > deadline:
        raise AssertionError("worker did not finish")
    time.sleep(0.1)
while not worker.quiescent():
    if time.monotonic() > deadline:
        raise AssertionError("worker did not become quiescent")
    time.sleep(0.1)
worker.advance(command)
assert store.read("import-worker") == first
verify_root(
    Path("/run/photo-wall-node-storage/app-roots") / ref.environment_sha256,
    ref,
    **{key: getattr(ref, key) for key in ("base_abi", "graphics_abi", "plugin_abi")},
)
print(
    "PASS exact manager-owned archive imported by bounded base worker; immutable result/root verified, completed recovery did not respawn, PID1 quiescent",
    flush=True,
)
store.close()
