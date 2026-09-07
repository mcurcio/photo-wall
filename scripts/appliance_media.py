"""Real upstream/native-media extension of the exact-artifact VM gate.

Boot and update authority remain in ApplianceE2E. This module owns only its
disposable upstream fixture, operator photo scenario and current-session reacquisition evidence.
"""

from __future__ import annotations

import datetime
import json
import re
import time
from pathlib import Path

from media.worker import load_connections
from scripts.boot_fixture import FixtureError, read_file, require, write_json
from scripts.immich_fixture import FixtureHost

MEDIA_TIMEOUT = 420
DENIAL_PROBE = """
import json, socket, sys
try:
    socket.getaddrinfo('immich', 2283)
    dns_denied = False
except socket.gaierror:
    dns_denied = True
try:
    with socket.create_connection((sys.argv[1], 2283), timeout=2):
        tcp_denied = False
except OSError:
    tcp_denied = True
print(json.dumps(dict(dns_denied=dns_denied, numeric_tcp_denied=tcp_denied)))
sys.exit(0 if dns_denied and tcp_denied else 1)
"""


class ApplianceMedia:
    def __init__(self, harness, worker_image: str):
        self.harness = harness
        self.worker_image = worker_image
        self.upstream = None
        self.setup = None
        self.photo = None

    def prepare(self) -> tuple[dict, Path]:
        h = self.harness
        h.report["media_setup_phase"] = "create"
        self.upstream = FixtureHost.create(h.state / "immich")
        h.report["media_setup_phase"] = "build"
        try:
            self.upstream.build(base_image=h.central_image)
        except Exception:
            raise FixtureError("media_fixture_build_failed") from None
        h.report["media_setup_phase"] = "start"
        self.upstream.compose("up", "-d", "--wait", "--wait-timeout", "240", timeout=600, capture=False)
        h.report["media_setup_phase"] = "topology"
        inventory, upstream_ip = self.upstream.topology()
        h.report["media_setup_phase"] = "initialize"
        initialized = self.upstream.role("setup", "initialize")
        h.report["media_setup_phase"] = "export"
        self.upstream.export_runtime()
        h.report["media_setup_phase"] = "connections"
        runtime = self.upstream.state / "runtime"
        connection = json.loads(read_file(runtime / "connection.json", 16384))
        private = h.state / "worker-connections.json"
        write_json(private, {"schema": 1, "connections": [connection]})
        private.chmod(0o600)
        connections = load_connections(private)
        require(set(connections) == {"fixture-library"}, "fixture_connection_scope")
        require(connections["fixture-library"].base_url == "http://immich:2283/api",
                "fixture_connection_origin")
        fixtures = json.loads(read_file(runtime / "fixtures.json", 65536))
        self.photo = fixtures["portrait"]
        require(self.photo["favorite"] is True and self.photo["deleted"] is False,
                "fixture_photo_invalid")
        network = self.upstream.project + "_upstream_net"
        network_id = h.run(["docker", "network", "inspect", "--format", "{{.Id}}", network],
                           timeout=20).decode().strip()
        require(re.fullmatch(r"[a-f0-9]{64}", network_id) is not None, "upstream_network_identity")
        self.upstream_ip = upstream_ip
        h.report["media"] = dict(worker_image=self.worker_image, upstream=initialized,
                                  upstream_inventory=inventory, presentations={}, rehydration={})
        h.report["media_setup_phase"] = "ready"
        return dict(worker_image=self.worker_image, upstream_project=self.upstream.project,
                    upstream_network_id=network_id, delivery_control=True), private

    def probe(self, action: str, player: dict, *args: str) -> dict:
        h = self.harness
        value = json.loads(h.run(["docker", "exec", h.fixture_central(), "python", "-m",
            "scripts.vm_media_probe", action, "--player-id", player["player_id"],
            "--epoch", str(player["authority_epoch"]), *args], timeout=30))
        require(isinstance(value, dict) and "error" not in value, "media_probe_failed")
        return value

    def configure(self, player: dict):
        captured = datetime.datetime.fromisoformat(self.photo["captured"].replace("Z", "+00:00")).timestamp()
        self.setup = self.probe("configure", player, "--captured-from", str(captured),
                                "--captured-until", str(captured + 1))
        require(set(self.setup) == {"frame_id", "output_id", "source_ref", "player_id", "authority_epoch",
                                   "starts_at"}, "media_configuration_invalid")
        self.harness.report["media"]["configuration"] = self.setup

    def wait_presentation(self, label: str, player: dict):
        h = self.harness
        deadline = time.monotonic() + MEDIA_TIMEOUT
        expected = h.report["media"]["presentations"].get("fresh", {}).get("sha256")
        args = ["--output-id", self.setup["output_id"], "--original-sha256", self.photo["sha256"]]
        if expected is not None:
            args += ["--expected-sha256", expected]
        grants = {}
        while time.monotonic() < deadline:
            require(h.checked_vm()["Running"], "vm_exited_during_media")
            value = self.probe("evidence", player, *args, "--prior-grants",
                               json.dumps(list(grants.values()), separators=(",", ":")))
            require(set(value) == {"presentations", "grants"} and isinstance(value["presentations"], list)
                    and len(value["presentations"]) <= 32, "media_evidence_invalid")
            require(isinstance(value["grants"], list) and len(value["grants"]) <= 32, "media_grants_invalid")
            for grant in value["grants"]:
                require(grant["player_id"] == player["player_id"]
                        and grant["authority_epoch"] == player["authority_epoch"]
                        and grant["valid"] is True, "media_grant_authority")
                key = tuple(grant[name] for name in ("plan_id", "revision", "assignment_id", "group_id"))
                if key not in grants:
                    if len(grants) == 64:
                        del grants[next(iter(grants))]
                    grants[key] = grant
            if value["presentations"]:
                proof = value["presentations"][0]
                require(proof["player_id"] == player["player_id"]
                        and proof["authority_epoch"] == player["authority_epoch"]
                        and proof["frame_id"] == self.setup["frame_id"]
                        and proof["output_id"] == self.setup["output_id"]
                        and proof["original_sha256"] == self.photo["sha256"]
                        and (expected is None or proof["sha256"] == expected), "media_evidence_mismatch")
                h.report["media"]["presentations"][label] = proof
                if label != "fresh":
                    self.verify_rehydration(label, proof)
                return proof
            time.sleep(2)
        raise FixtureError("native_photo_presentation_timeout")

    def network_denial(self, label: str):
        """Test the VM's outer egress namespace, without giving upstream data to its Player."""
        h = self.harness
        h.checked_vm()
        worker = h.fixture.project + "-worker"
        h.fixture.check(h.fixture.resources["container:" + worker])
        expected = {
            h.name: {h.fixture.project + "-front"},
            h.fixture_central(): {h.fixture.project + "-front", h.fixture.project + "-database"},
            worker: {h.fixture.project + "-database", self.upstream.project + "_upstream_net"},
        }
        for name, networks in expected.items():
            actual = json.loads(h.run(["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}",
                                       name], timeout=20))
            require(set(actual) == networks, "media_network_boundary_changed")
        result = json.loads(h.run(["docker", "exec", h.name, "python3.12", "-c", DENIAL_PROBE,
                                   self.upstream_ip], timeout=15))
        require(result == dict(dns_denied=True, numeric_tcp_denied=True), "vm_egress_reaches_upstream")
        h.report["media"].setdefault("vm_egress_namespace_denial", {})[label] = result

    def verify_rehydration(self, label: str, proof: dict):
        from scripts.test_appliance_e2e import serial_records
        from scripts.vm_cache_evidence import rehydration

        h = self.harness
        logs = h.run(["docker", "logs", "--since", h.boot_started_at, "--tail", "10000",
                      h.fixture_central()], timeout=20).decode(errors="replace")
        expected = dict(event="photo-wall-fixture-media-delivery", sha256=proof["sha256"],
                        player_id=proof["player_id"], authority_epoch=proof["authority_epoch"])
        delivered = any(value == expected for value in serial_records(logs))
        value = rehydration(h.report["media"]["presentations"]["fresh"], proof,
                            h.report["boots"][0], h.report["boots"][-1], delivery_observed=delivered)
        h.report["media"]["rehydration"][label] = value

    def down(self):
        errors = []
        for callback in (self.upstream.cleanup if self.upstream else None,):
            if callback is not None:
                try:
                    callback()
                except Exception:
                    errors.append("media_resource_cleanup_failed")
        if errors:
            raise FixtureError("media_resource_cleanup_failed")
