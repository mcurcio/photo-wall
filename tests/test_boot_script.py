"""Shell-level tests for the netboot boot script (0014 rev 5, design §2.8):
the `panic()` redefinition and the emergency-restart exit, run with no
hardware under the ash family the initramfs `/bin/sh` belongs to (klibc sh or
busybox ash): `dash` always, `busybox sh` where installed. Never the host's
`sh`, which may be bash -- bash survives a failed redirection on the `:`
special built-in, where the ash family exits the whole shell (PID 1 on the
device). The watchdog device paths and the sysrq trigger are
redirected to temp files via the `PHOTOWALL_*` environment overrides the
script itself reads (`${VAR:-default}`), and `PHOTOWALL_RESTART_SLEEP=0`
skips the real console-readability pause so the test does not have to wait
5 real seconds.

`mountroot()`'s own `/usr/bin/python3 -I -m appliance.netboot_init` call is
NOT exercised here (an absolute path a shell function cannot shadow, and
whose real behaviour would depend on what interpreter the test host happens
to have at that path) -- `false || photowall_restart` below is the same
"a failing init calls photowall_restart" shape `mountroot` uses, without
depending on a real Python invocation succeeding or failing incidentally.
"""

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path("appliance/netboot_initramfs/scripts/photowall-netboot")


def _dash() -> list[str]:
    dash = shutil.which("dash")
    if dash is None:
        # Debian and Ubuntu always ship dash (the CI runners' /bin/sh): missing there is a
        # broken check, not a reason to pass.
        if sys.platform == "linux":
            pytest.fail("dash is required to test the boot script's shell semantics")
        pytest.skip("dash is not installed")
    return [dash]


def _busybox_sh() -> list[str]:
    busybox = shutil.which("busybox")
    if busybox is None:
        pytest.skip("busybox is not installed")
    return [busybox, "sh"]


@pytest.fixture(params=[_dash, _busybox_sh], ids=["dash", "busybox-sh"])
def shell(request) -> list[str]:
    """The ash-family shell the boot script runs under in the test."""
    return request.param()


def _run(shell: list[str], command: str, *, env_overrides: dict[str, str],
         timeout: float = 2.0) -> str:
    """Run `command` under `shell` in its own process group, killing the WHOLE
    group after `timeout` -- `photowall_restart` never returns (its final
    `sleep 60` loop is a child process that would otherwise keep the stdout
    pipe open past a plain `process.kill()`, hanging `communicate()` forever)."""
    env = {"PATH": "/usr/bin:/bin", **env_overrides}
    process = subprocess.Popen([*shell, "-c", command], env=env, start_new_session=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        stdout, _ = process.communicate()
    return stdout


def _env(tmp_path, *, watchdog="watchdog0") -> dict[str, str]:
    return {
        "PHOTOWALL_SYSRQ_TRIGGER": str(tmp_path / "sysrq-trigger"),
        "PHOTOWALL_WATCHDOG_PATHS": str(tmp_path / watchdog),
        "PHOTOWALL_RESTART_SLEEP": "0",
    }


def test_photowall_restart_writes_b_to_the_sysrq_trigger_and_opens_the_watchdog(tmp_path, shell):
    env = _env(tmp_path)
    (tmp_path / "sysrq-trigger").write_text("")
    _run(shell, f". '{SCRIPT}'; photowall_restart", env_overrides=env)
    assert (tmp_path / "sysrq-trigger").read_text() == "b\n"
    # The guarded open (arm-if-stage-1-never-did, then a bare close) touched
    # the first candidate path -- no assertion that it pets zero times: the
    # close itself pings once, by design (design §2.8).
    assert (tmp_path / "watchdog0").exists()


def test_photowall_restart_falls_back_to_the_second_watchdog_path(tmp_path, shell):
    # The first candidate's PARENT directory does not exist, so the open
    # genuinely fails (ENOENT), unlike a merely-absent file in a writable
    # directory (which `: >path` would just create) -- the guarded loop must
    # try the next path rather than stop.
    env = _env(tmp_path)
    env["PHOTOWALL_WATCHDOG_PATHS"] = f"{tmp_path / 'no-such-dir' / 'watchdog0'} {tmp_path / 'watchdog'}"
    (tmp_path / "sysrq-trigger").write_text("")
    _run(shell, f". '{SCRIPT}'; photowall_restart", env_overrides=env)
    assert (tmp_path / "sysrq-trigger").read_text() == "b\n"
    assert (tmp_path / "watchdog").exists()


def test_an_unopenable_path_fails_the_probe_without_ending_the_shell(tmp_path, shell):
    """`photowall_can_open` is also `mountroot`'s `/dev/console` probe: a path
    that cannot be opened answers false and the shell (PID 1 on the device)
    carries on; one that can is opened (created here) and answers true."""
    stdout = _run(
        shell,
        f". '{SCRIPT}'; "
        f"photowall_can_open '{tmp_path / 'no-such-dir' / 'console'}' || echo refused; "
        f"photowall_can_open '{tmp_path / 'console'}' && echo opened",
        env_overrides=_env(tmp_path),
    )
    assert stdout.split() == ["refused", "opened"]
    assert (tmp_path / "console").read_text() == ""


def test_a_failing_init_reaches_photowall_restart_and_the_sysrq_trigger(tmp_path, shell):
    """S0-AC8(a): the shape `mountroot` uses -- `if ! <init>; then ...;
    photowall_restart; fi` -- reaches the trigger when the init command
    fails, with `false` standing in for a failing init."""
    env = _env(tmp_path)
    (tmp_path / "sysrq-trigger").write_text("")
    stdout = _run(
        shell,
        f". '{SCRIPT}'; "
        "if ! false; then echo 'photo-wall: netboot_failed'; photowall_restart; fi",
        env_overrides=env,
    )
    assert "photo-wall: netboot_failed" in stdout
    assert (tmp_path / "sysrq-trigger").read_text() == "b\n"


def test_panic_redefined_after_a_stub_scripts_functions_wins_over_the_stub(tmp_path, shell):
    """S0-AC8(b): a stub `/scripts/functions` (initramfs-tools' OWN panic) is
    sourced first, then our script -- so a LATER `panic "No init found"` call
    (init's own, after mountroot returns) uses OUR redefinition, not the
    stub's, and reaches the sysrq trigger."""
    env = _env(tmp_path)
    (tmp_path / "sysrq-trigger").write_text("")
    functions_stub = tmp_path / "functions"
    functions_stub.write_text('panic() { echo "stub-panic: $*"; }\n')
    stdout = _run(
        shell,
        f". '{functions_stub}'; . '{SCRIPT}'; panic 'No init found'",
        env_overrides=env,
    )
    assert "No init found" in stdout
    assert "stub-panic" not in stdout
    assert (tmp_path / "sysrq-trigger").read_text() == "b\n"
