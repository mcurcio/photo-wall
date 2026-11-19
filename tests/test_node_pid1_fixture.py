"""The real-PID1 scenarios' Central half without Docker (tests/node_pid1_central_fixture.py): the
real app beside the worker's Node bus, its hub and its job runtime, on a disposable database.

With no Node on the hub: the PXE stand-in's boot offer answers for the Pi's serial; Install of a
phase's release answers "restarting" and queues one `RebootPi`, which the worker's job runtime runs
and records undelivered (no link), read back through GET /fixture/pi; the outage makes Central's
node routes answer 503 and its end lets them answer again. The scenarios themselves need Docker
(tests/test_node_pid1.py; node-pid1.yml in CI).
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import asdict, replace
from uuid import uuid4

from node_pid1_central_fixture import REBOOT_SECONDS, central_fixture
from test_fleet_attempts import SERIAL
from test_node_boot import environment

from contracts.node_boot import NodeBootRequestV2, encode_node_boot_request


def _components(directory):
    """A components directory as tests/node_pid1_fixture/build.sh lays it out, with small archives
    whose references name their exact bytes; returns (directory, the success phase's ref, archive)."""
    directory.mkdir()
    refs = {}
    for name, deb in (("app", "photo-wall-player"), ("manager-primary", "photo-wall-app-manager"),
                      ("success", "photo-wall-player")):
        data = f"archive of {name}".encode()
        (directory / f"{name}.squashfs").write_bytes(data)
        refs[name] = replace(environment("d", deb),
                             environment_sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
    (directory / "components.json").write_text(json.dumps(
        {"app_environment": asdict(refs["app"]), "manager_primary": asdict(refs["manager-primary"])}))
    return directory, refs["success"], directory / "success.squashfs"


def _call(fixture, method, path, body=None, *, raw=None, admin=False):
    headers = {"Content-Type": "application/json", "X-Fixture-Token": fixture["fixture_token"]}
    if admin:
        headers["Authorization"] = "Bearer " + fixture["admin_token"]
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    request = urllib.request.Request(fixture["host_origin"] + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def test_the_fixture_offers_installs_records_the_reboot_and_takes_central_away(registry, tmp_path):
    """Mutation probe: without the worker's job runtime in worker_bus_and_hub, the reboot is never
    recorded and the wait fails."""
    components, success, archive = _components(tmp_path / "components")
    with central_fixture(registry, components, {"success": (success, archive)}, tmp_path / "central",
                         "127.0.0.1", hub=True) as fixture:
        status, ready = _call(fixture, "POST", "/fixture/node-ready", {"boot_id": str(uuid4())})
        assert status == 200, ready
        assert ready["serial"] == SERIAL and ready["offer"]["serial"] == SERIAL, ready

        tag = fixture["tags"]["success"]
        status, started = _call(fixture, "POST", f"/v1/operator/players/{SERIAL}/install", {"version": tag},
                                admin=True)
        assert (status, started["install"]) == (200, "restarting"), started

        deadline = time.monotonic() + REBOOT_SECONDS
        while True:
            status, pi = _call(fixture, "GET", "/fixture/pi")
            assert status == 200, pi
            if pi["reboots"] and pi["reboots"][0]["delivered"] is not None:
                break
            assert time.monotonic() < deadline, pi
            time.sleep(0.25)
        assert [(r["reason"], r["delivered"]) for r in pi["reboots"]] == [(f"install:{tag}", False)], pi
        assert pi["version"] == tag and pi["linked"] is False, pi
        assert pi["frame"] == {"generation": 0, "player_id": None, "output_id": None}, pi
        assert _call(fixture, "GET", "/fixture/host") == (200, {"facts": None, "metrics": []})

        offer = encode_node_boot_request(NodeBootRequestV2(SERIAL, uuid4(), "a" * 64))
        assert _call(fixture, "POST", "/fixture/outage", {"active": True})[1]["active"] is True
        assert _call(fixture, "POST", "/v2/node/boot-offers", raw=offer)[0] == 503
        assert _call(fixture, "POST", "/fixture/outage", {"active": False})[1]["active"] is False
        assert _call(fixture, "POST", "/v2/node/boot-offers", raw=offer)[0] == 200
