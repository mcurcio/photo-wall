"""scripts/player_start_probe.py, the CI guard that starts the Player unit on the built base under
real systemd. Its real run needs root, docker and an arm64 host (base-image.yml); here every
docker command is faked, so these pin the probe's own logic: the pass condition, the named
spawn failure, the base checks, that the start is skipped when dpkg refuses the package, and
that the container is always removed."""

from __future__ import annotations

import itertools
import subprocess
from pathlib import Path

import pytest

from appliance.provision import DEFAULT_UNIT
from player import service as player_service
from scripts.player_start_probe import (
    DEB_IN_CONTAINER,
    HANDOFF_IN_CONTAINER,
    PROBE_SERIAL,
    PROVISION_UNIT,
    SYSTEMD,
    Container,
    cpuinfo_text,
    docker_run_argv,
    group_violations,
    import_squashfs,
    membership_violations,
    probe,
    start_violations,
    udev_violations,
)

ACTIVE = {"ActiveState": "active", "SubState": "running", "Result": "success",
          "ExecMainCode": "0", "ExecMainStatus": "0"}
# v0.9.1's Player on v0.9.1's base, as systemd 257 showed it.
GROUP_FAILURE = {"ActiveState": "activating", "SubState": "auto-restart", "Result": "exit-code",
                 "ExecMainCode": "1", "ExecMainStatus": "216"}
GETENT_ALL = "render:x:992:\nvideo:x:44:\ninput:x:996:\n"


# --- the pass condition ----------------------------------------------------------------------

def test_a_started_active_unit_passes():
    assert start_violations(0, ACTIVE) == []


def test_the_v0_9_1_group_failure_is_named_as_a_spawn_failure():
    assert start_violations(1, GROUP_FAILURE) == [
        f"{DEFAULT_UNIT} failed at spawn, before the Player ran: status=216/GROUP "
        "(result exit-code)"]


@pytest.mark.parametrize("status,name", [("217", "USER"), ("203", "EXEC"),
                                         ("226", "NAMESPACE")])
def test_every_systemd_spawn_status_is_a_spawn_failure(status, name):
    [violation] = start_violations(1, {**GROUP_FAILURE, "ExecMainStatus": status})
    assert f"failed at spawn, before the Player ran: status={status}/{name}" in violation


def test_a_player_that_ran_and_failed_does_not_pass():
    """Past spawn is not enough: provisioning's own `systemctl start` waits for READY=1."""
    [violation] = start_violations(1, {**GROUP_FAILURE, "ExecMainStatus": "1"})
    assert violation == (f"{DEFAULT_UNIT} did not start: systemctl start returned 1; unit "
                         "activating/auto-restart, result exit-code, status=1")


def test_a_start_that_outlives_its_bound_does_not_pass():
    [violation] = start_violations(None, {**ACTIVE, "ActiveState": "activating",
                                          "SubState": "start"})
    assert "systemctl start timed out; unit activating/start" in violation


def test_a_zero_return_with_the_unit_not_running_does_not_pass():
    assert start_violations(0, {**ACTIVE, "SubState": "exited"}) != []


# --- the base checks -------------------------------------------------------------------------

def test_every_device_group_present_passes():
    assert group_violations(GETENT_ALL) == []


def test_a_base_without_udev_lacks_render_and_input():
    """v0.9.1's base: base-passwd gives video; only udev's sysusers give render and input."""
    assert group_violations("video:x:44:\n") == [
        "group render does not exist (udev creates it)",
        "group input does not exist (udev creates it)"]


def test_membership_reads_id_output():
    assert membership_violations("wall video input render\n") == []
    assert membership_violations("wall video\n") == ["wall is not in group render",
                                                     "wall is not in group input"]


@pytest.mark.parametrize("status,ok", [("install ok installed", True),
                                       ("deinstall ok config-files", False), ("", False)])
def test_udev_must_be_installed(status, ok):
    assert (udev_violations(status) == []) is ok


# --- the container ---------------------------------------------------------------------------

def test_the_container_boots_systemd_with_the_provisioner_masked(tmp_path):
    argv = docker_run_argv("base:probe", "probe-1", tmp_path / "cpuinfo")
    assert argv[:2] == ["docker", "run"] and "--privileged" in argv
    assert f"{tmp_path / 'cpuinfo'}:/proc/cpuinfo:ro" in argv
    assert argv[-3:] == ["base:probe", SYSTEMD, f"systemd.mask={PROVISION_UNIT}"]


def test_the_cpuinfo_fixture_is_the_identity_the_player_reads(tmp_path, monkeypatch):
    """Bound to the reader itself, so a Player that stops reading cpuinfo's Serial fails here,
    not as a red base build with no named cause."""
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text(cpuinfo_text())
    monkeypatch.setattr(player_service, "PI_SERIAL_PATH", tmp_path / "no-devicetree")
    monkeypatch.setattr(player_service, "CPUINFO_PATH", cpuinfo)
    assert player_service.read_pi_serial().strip() == PROBE_SERIAL.encode()


class FakeDocker:
    """Answers each docker command from `replies` (a function of argv), recording every call."""

    def __init__(self, replies):
        self.calls, self.replies = [], replies

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        stdout, code = self.replies(list(argv))
        if kwargs.get("check") and code:
            raise subprocess.CalledProcessError(code, argv, stdout, "")
        return subprocess.CompletedProcess(argv, code, stdout, "")


def in_container(argv):
    """The command a `docker exec` runs: what follows the probe's container name."""
    name = next(item for item in argv if item.startswith("photo-wall-player-start-probe-"))
    return argv[argv.index(name) + 1:]


def base_replies(*, udev=True, dpkg_code=0, start_code=0, shown=ACTIVE, booted="degraded"):
    def reply(argv):
        if argv[1] != "exec":
            return "", 0
        match in_container(argv)[:2]:
            case ["systemctl", "is-system-running"]:
                return booted + "\n", 0
            case ["dpkg-query", _]:
                return ("install ok installed" if udev else ""), 0 if udev else 1
            case ["getent", "group"]:
                return (GETENT_ALL if udev else "video:x:44:\n"), 0 if udev else 2
            case ["dpkg", "--install"]:
                return "Setting up photo-wall-player ...\n", dpkg_code
            case ["id", "-nG"]:
                return ("wall video render input\n" if udev else "wall video\n"), 0
            case ["systemctl", "start"]:
                return "", start_code
            case ["systemctl", "show"]:
                return "".join(f"{key}={value}\n" for key, value in shown.items()), 0
        return "", 0
    return reply


def test_a_good_base_passes_and_the_container_is_removed(tmp_path):
    docker = FakeDocker(base_replies())
    assert probe("base:probe", tmp_path / "player.deb", tmp_path, run=docker) == []
    name = docker.calls[0][docker.calls[0].index("--name") + 1]
    assert ["docker", "cp", str(tmp_path / "player.deb"), f"{name}:{DEB_IN_CONTAINER}"] in \
        docker.calls
    assert ["docker", "cp", str(tmp_path / "public.json"), f"{name}:{HANDOFF_IN_CONTAINER}"] in \
        docker.calls
    assert ["docker", "exec", "--env", "DEBIAN_FRONTEND=noninteractive", name, "dpkg",
            "--install", DEB_IN_CONTAINER] in docker.calls
    assert ["docker", "exec", name, "systemctl", "start", DEFAULT_UNIT] in docker.calls
    assert docker.calls[-1] == ["docker", "rm", "--force", name]


def test_the_handoff_is_provisionings_own_naming_a_central_that_never_resolves(tmp_path):
    probe("base:probe", tmp_path / "player.deb", tmp_path, run=FakeDocker(base_replies()))
    assert player_service.load_config(tmp_path / "public.json").central_origin == \
        "http://central.invalid"


def test_v0_9_1s_base_fails_naming_udev_the_groups_and_the_spawn_failure(tmp_path):
    docker = FakeDocker(base_replies(udev=False, start_code=1, shown=GROUP_FAILURE))
    violations = probe("base:probe", tmp_path / "player.deb", tmp_path, run=docker)
    assert violations == [
        "udev is not installed (dpkg status: none)",
        "group render does not exist (udev creates it)",
        "group input does not exist (udev creates it)",
        "wall is not in group render",
        "wall is not in group input",
        f"{DEFAULT_UNIT} failed at spawn, before the Player ran: status=216/GROUP "
        "(result exit-code)"]
    assert any(call[-4:] == ["-n", "40", "-u", DEFAULT_UNIT] for call in docker.calls)
    assert docker.calls[-1][:3] == ["docker", "rm", "--force"]


def test_a_refused_install_is_not_started(tmp_path):
    docker = FakeDocker(base_replies(dpkg_code=1))
    violations = probe("base:probe", tmp_path / "player.deb", tmp_path, run=docker)
    assert violations == ["dpkg --install of the Player failed (exit 1)"]
    assert not any(in_container(call)[:2] == ["systemctl", "start"]
                   for call in docker.calls if call[1] == "exec")


def test_a_base_that_never_boots_fails_and_is_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(Container, "wait_booted", lambda self, **_: "starting")
    docker = FakeDocker(base_replies())
    violations = probe("base:probe", tmp_path / "player.deb", tmp_path, run=docker)
    assert violations == ["the base did not boot under systemd (is-system-running: starting)"]
    assert docker.calls[-1][:3] == ["docker", "rm", "--force"]


def test_a_failing_step_still_removes_the_container(tmp_path):
    def reply(argv):
        if argv[1] == "cp":
            return "no such container", 1
        return base_replies()(argv)
    docker = FakeDocker(reply)
    with pytest.raises(subprocess.CalledProcessError):
        probe("base:probe", tmp_path / "player.deb", tmp_path, run=docker)
    assert docker.calls[-1][:3] == ["docker", "rm", "--force"]


def test_wait_booted_polls_until_systemd_answers():
    answers = iter([("", 1), ("", 1), ("degraded\n", 1)])
    sleeps = []
    container = Container("c", run=FakeDocker(lambda argv: next(answers)))
    assert container.wait_booted(sleep=sleeps.append, clock=lambda: 0.0) == "degraded"
    assert sleeps == [1.0, 1.0]


def test_wait_booted_gives_up_at_its_deadline():
    times, sleeps = itertools.count(0.0, 4.0), []
    container = Container("c", run=FakeDocker(lambda argv: ("starting\n", 1)))
    assert container.wait_booted(seconds=10.0, sleep=sleeps.append,
                                 clock=lambda: next(times)) == "starting"
    assert sleeps == [1.0]


def test_import_squashfs_extracts_tars_imports_and_cleans_up(tmp_path):
    docker = FakeDocker(lambda argv: ("", 0))
    import_squashfs(Path("/base.squashfs"), "base:probe", tmp_path, run=docker)
    root, archive = str(tmp_path / "rootfs"), str(tmp_path / "rootfs.tar")
    assert docker.calls == [
        ["unsquashfs", "-no-progress", "-no-xattrs", "-d", root, "/base.squashfs"],
        ["tar", "--numeric-owner", "-C", root, "-cf", archive, "."],
        ["docker", "import", archive, "base:probe"]]
    assert not (tmp_path / "rootfs").exists() and not (tmp_path / "rootfs.tar").exists()
