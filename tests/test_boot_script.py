"""Shell-level tests for the netboot boot script (0014 rev 5, design §2.8):
the `panic()` redefinition and the emergency-restart exit, run under `sh`
with no hardware -- the watchdog device paths and the sysrq trigger are
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
import signal
import subprocess
from pathlib import Path

SCRIPT = Path("appliance/netboot_initramfs/scripts/photowall-netboot")


def _run(command: str, *, env_overrides: dict[str, str], timeout: float = 2.0) -> str:
    """Run `command` under `sh` in its own process group, killing the WHOLE
    group after `timeout` -- `photowall_restart` never returns (its final
    `sleep 60` loop is a child process that would otherwise keep the stdout
    pipe open past a plain `process.kill()`, hanging `communicate()` forever)."""
    env = {"PATH": "/usr/bin:/bin", **env_overrides}
    process = subprocess.Popen(["sh", "-c", command], env=env, start_new_session=True,
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


def test_photowall_restart_writes_b_to_the_sysrq_trigger_and_opens_the_watchdog(tmp_path):
    env = _env(tmp_path)
    (tmp_path / "sysrq-trigger").write_text("")
    _run(f". '{SCRIPT}'; photowall_restart", env_overrides=env)
    assert (tmp_path / "sysrq-trigger").read_text() == "b\n"
    # The guarded open (arm-if-stage-1-never-did, then a bare close) touched
    # the first candidate path -- no assertion that it pets zero times: the
    # close itself pings once, by design (design §2.8).
    assert (tmp_path / "watchdog0").exists()


def test_photowall_restart_falls_back_to_the_second_watchdog_path(tmp_path):
    # The first candidate's PARENT directory does not exist, so the open
    # genuinely fails (ENOENT), unlike a merely-absent file in a writable
    # directory (which `: >path` would just create) -- the guarded loop must
    # try the next path rather than stop.
    env = _env(tmp_path)
    env["PHOTOWALL_WATCHDOG_PATHS"] = f"{tmp_path / 'no-such-dir' / 'watchdog0'} {tmp_path / 'watchdog'}"
    (tmp_path / "sysrq-trigger").write_text("")
    _run(f". '{SCRIPT}'; photowall_restart", env_overrides=env)
    assert (tmp_path / "sysrq-trigger").read_text() == "b\n"
    assert (tmp_path / "watchdog").exists()


def test_a_failing_init_reaches_photowall_restart_and_the_sysrq_trigger(tmp_path):
    """S0-AC8(a): the shape `mountroot` uses -- `if ! <init>; then ...;
    photowall_restart; fi` -- reaches the trigger when the init command
    fails, with `false` standing in for a failing init."""
    env = _env(tmp_path)
    (tmp_path / "sysrq-trigger").write_text("")
    stdout = _run(
        f". '{SCRIPT}'; "
        "if ! false; then echo 'photo-wall: netboot_failed'; photowall_restart; fi",
        env_overrides=env,
    )
    assert "photo-wall: netboot_failed" in stdout
    assert (tmp_path / "sysrq-trigger").read_text() == "b\n"


def test_panic_redefined_after_a_stub_scripts_functions_wins_over_the_stub(tmp_path):
    """S0-AC8(b): a stub `/scripts/functions` (initramfs-tools' OWN panic) is
    sourced first, then our script -- so a LATER `panic "No init found"` call
    (init's own, after mountroot returns) uses OUR redefinition, not the
    stub's, and reaches the sysrq trigger."""
    env = _env(tmp_path)
    (tmp_path / "sysrq-trigger").write_text("")
    functions_stub = tmp_path / "functions"
    functions_stub.write_text('panic() { echo "stub-panic: $*"; }\n')
    stdout = _run(
        f". '{functions_stub}'; . '{SCRIPT}'; panic 'No init found'",
        env_overrides=env,
    )
    assert "No init found" in stdout
    assert "stub-panic" not in stdout
    assert (tmp_path / "sysrq-trigger").read_text() == "b\n"
