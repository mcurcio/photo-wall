"""The resident gate's HTTP peer and process-continuity pass condition.

The arm64 base-image job runs systemd; these focused tests keep the recorder and
refusal assertions honest on hosts without the built squashfs or systemd.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request

import pytest

from scripts.os_agent_service_probe import (
    AGENT_LAUNCHER,
    ProbeError,
    Recorder,
    ServiceIdentity,
    continuity_violations,
    service_identity,
    start_ticks,
)


def request(url, *, body=None):
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=2) as reply:
        return reply.status, reply.read()


def test_local_peer_serves_corrupt_package_and_accepts_only_schema_two():
    good = b"a built Player deb fixture"
    with Recorder(good) as recorder:
        _, identity = request(recorder.origin + "/v1/locate")
        assert json.loads(identity) == {"service": "photo-wall-central", "api": 1}
        _, manifest = request(recorder.origin + "/v1/app/manifest")
        offer = json.loads(manifest)
        assert offer["sha256"] == recorder.digest and offer["size"] == len(good)
        _, corrupt = request(recorder.origin + f"/v1/app/package/{recorder.digest}.deb")
        assert len(corrupt) == len(good) and corrupt != good
        _, receipt = request(recorder.origin + "/v2/appliance/check-ins",
                             body={"schema": 2, "observation_sequence": 1})
        assert json.loads(receipt) == {"accepted": True}
        with pytest.raises(urllib.error.HTTPError) as refusal:
            request(recorder.origin + "/v1/appliance/check-ins", body={"schema": 1})
        assert refusal.value.code == 404
        assert len(recorder.snapshot()["observations"]) == 1


def test_continuity_requires_same_resident_process_and_post_refusal_report():
    identity = ServiceIdentity(42, 900, "a" * 32)
    before = {"observation_sequence": 1, "agent_incarnation": "b" * 32,
              "phase": "base_ready"}
    after = {"observation_sequence": 2, "agent_incarnation": "b" * 32,
             "phase": "retry_wait", "fault_code": "app_integrity",
             "attempted_app_sha256": "c" * 64}
    package = "/v1/app/package/" + "c" * 64 + ".deb"
    assert continuity_violations(before, after, "c" * 64, identity, identity,
                                 [package]) == []
    violations = continuity_violations(before, {**after, "observation_sequence": 1,
                                                    "agent_incarnation": "d" * 32},
                                       "c" * 64, identity, ServiceIdentity(42, 901, "a" * 32),
                                       ["/v1/appliance/check-ins"])
    assert set(violations) == {"agent_process_changed", "agent_sequence_did_not_advance",
                               "agent_incarnation_changed", "package_was_not_fetched",
                               "agent_fell_back_to_v1"}


def test_service_identity_is_the_installed_unit_and_exact_launcher():
    pid = 42
    properties = ("ActiveState=active\nSubState=running\nMainPID=42\n"
                  f"InvocationID={'a' * 32}\n"
                  "FragmentPath=/lib/systemd/system/photo-wall-os-agent.service\n")
    stat = "42 (python3) S " + " ".join(["0"] * 18 + ["900"] + ["0"] * 2)

    class Container:
        def exec(self, *argv):
            if argv[:2] == ("systemctl", "show"):
                body = properties
            elif argv == ("cat", f"/proc/{pid}/cmdline"):
                body = f"/usr/bin/python3\0-I\0-B\0{AGENT_LAUNCHER}\0"
            elif argv == ("cat", f"/proc/{pid}/stat"):
                body = stat
            else:
                raise AssertionError(argv)
            return subprocess.CompletedProcess(argv, 0, body, "")

    assert start_ticks(stat) == 900
    assert service_identity(Container()) == ServiceIdentity(pid, 900, "a" * 32)

    class WrongLauncher(Container):
        def exec(self, *argv):
            value = super().exec(*argv)
            if argv == ("cat", f"/proc/{pid}/cmdline"):
                return subprocess.CompletedProcess(argv, 0, "python3\0-m\0probe\0", "")
            return value

    with pytest.raises(ProbeError, match="agent_not_installed_launcher"):
        service_identity(WrongLauncher())
