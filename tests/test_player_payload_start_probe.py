"""The schema-2 CI probe must exercise the base executor, not the legacy installer."""

import hashlib
import subprocess

from scripts import player_start_probe as probe_module

ABI = "sha256:" + "a" * 64


class Docker:
    def __init__(self, *, activation=("committed\n", 0)):
        self.calls = []
        self.activation = activation

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:2] == ["docker", "exec"]:
            name = next(part for part in argv if part.startswith(
                "photo-wall-player-start-probe-"))
            command = argv[argv.index(name) + 1:]
            if command[:2] == ["systemctl", "is-system-running"]:
                result = ("degraded\n", 0)
            elif command[:2] == ["dpkg-query", "-W"]:
                result = ("install ok installed", 0)
            elif command[:2] == ["getent", "group"]:
                result = ("render:x:992:\nvideo:x:44:\ninput:x:996:\n", 0)
            elif command[:2] == ["id", "-nG"]:
                result = ("wall render video input\n", 0)
            elif command[:3] == ["python3", "-I", "-c"]:
                result = self.activation
            elif command[:2] == ["systemctl", "show"]:
                result = ("ActiveState=active\nSubState=running\nResult=success\n"
                          "ExecMainCode=0\nExecMainStatus=0\n", 0)
            else:
                result = ("", 0)
        else:
            result = ("", 0)
        return subprocess.CompletedProcess(argv, result[1], result[0], "activation error")


def test_schema_two_probe_activates_exact_archive_and_never_installs_deb(tmp_path,
                                                                         monkeypatch):
    payload = tmp_path / "player-payload.tar.gz"
    payload.write_bytes(b"built payload bytes")
    monkeypatch.setattr(probe_module, "verify_archive", lambda path: {"base_abi": ABI})
    docker = Docker()

    assert probe_module.probe("base:probe", None, tmp_path, payload=payload, run=docker) == []

    commands = [call[call.index(next(part for part in call if part.startswith(
        "photo-wall-player-start-probe-"))) + 1:] for call in docker.calls
        if call[:2] == ["docker", "exec"]]
    [activation] = [cmd for cmd in commands if cmd[:3] == ["python3", "-I", "-c"]]
    assert activation[3] == probe_module.PAYLOAD_ACTIVATION
    assert activation[4:] == [probe_module.PAYLOAD_IN_CONTAINER,
                              hashlib.sha256(payload.read_bytes()).hexdigest(),
                              str(payload.stat().st_size), ABI]
    assert not any(cmd[:2] == ["dpkg", "--install"] for cmd in commands)
    name = docker.calls[0][docker.calls[0].index("--name") + 1]
    assert ["docker", "cp", str(payload),
            f"{name}:{probe_module.PAYLOAD_IN_CONTAINER}"] in docker.calls
    assert docker.calls[-1] == ["docker", "rm", "--force", name]


def test_schema_two_probe_fails_if_base_executor_does_not_commit(tmp_path, monkeypatch):
    payload = tmp_path / "player-payload.tar.gz"
    payload.write_bytes(b"built payload bytes")
    monkeypatch.setattr(probe_module, "verify_archive", lambda path: {"base_abi": ABI})
    docker = Docker(activation=("rolled_back\n", 0))

    violations = probe_module.probe("base:probe", None, tmp_path,
                                    payload=payload, run=docker)

    assert len(violations) == 1
    assert "data-only Player activation failed" in violations[0]
    assert docker.calls[-1][1:3] == ["rm", "--force"]
