"""Stage-1 liveness (0014 rev 5, design §2.8): the hardware watchdog keeper,
the masking rule, and the kernel liveness cmdline check. Real device I/O
(`fcntl.ioctl`, `os.open` on a character device) is exercised through fakes
here -- `tests/test_boot_script.py` covers the shell-side emergency restart,
and `test_netboot_init.py` covers `netboot()`'s wiring of a `Keeper`."""

import ast
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from appliance.bootstrap import (
    KERNEL_LIVENESS,
    MIN_MASKED_SECONDS,
    REBOOT_WATCHDOG_SECONDS,
    RUNTIME_WATCHDOG_SECONDS,
    STAGE1_BUDGET,
    STAGE1_WATCHDOG_TIMEOUT,
    WATCHDOG_TIMEOUTS,
    arm_watchdog,
    handover_dropin,
    masked_hardware_seconds,
    missing_kernel_liveness,
)
from appliance.netboot_init import (
    BASE_FETCH_SECONDS,
    DEBUG_PAUSE_SECONDS,
    NETWORKING_TIMEOUT_SECONDS,
)
from uplink.clock import GATE_BUDGET
from uplink.fetch import READ_TIMEOUT
from uplink.locate import LOCATE_DEADLINE


class ManualMonotonic:
    """A moving fake `time.monotonic`, advanced explicitly by tests."""

    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeDevice:
    """A `WatchdogDevice` double: records every call; never actually opens a
    character device."""

    def __init__(self, *, accepted=None, fail_set_timeout=False):
        self.accepted = accepted
        self.fail_set_timeout = fail_set_timeout
        self.set_timeout_calls: list[int] = []
        self.keepalive_calls = 0
        self.released = False

    def set_timeout(self, seconds: int) -> int:
        self.set_timeout_calls.append(seconds)
        if self.fail_set_timeout:
            raise OSError("driver refused")
        return self.accepted if self.accepted is not None else seconds

    def keepalive(self) -> None:
        self.keepalive_calls += 1

    def release(self) -> None:
        self.released = True


# --- S0-AC1, S0-AC2: arm_watchdog ------------------------------------------

def test_arm_watchdog_sets_timeout_then_pets_once():
    device = FakeDevice()
    arm_watchdog(device=device, monotonic=ManualMonotonic())
    assert device.set_timeout_calls == [STAGE1_WATCHDOG_TIMEOUT]
    assert device.keepalive_calls == 1


def test_arm_watchdog_with_no_device_is_unarmed(tmp_path):
    dropin = tmp_path / "dropin.conf"
    keeper = arm_watchdog(device=None, dropin=dropin, monotonic=ManualMonotonic())
    assert keeper.summary == "unarmed reason=no_device"
    keeper.pet()  # no device to call
    keeper.hand_over()
    assert dropin.read_bytes() == handover_dropin()


def test_arm_watchdog_summary_names_a_smaller_accepted_timeout():
    device = FakeDevice(accepted=90)
    keeper = arm_watchdog(device=device, monotonic=ManualMonotonic())
    assert keeper.summary == "armed device=device timeout=90s"


def test_arm_watchdog_set_timeout_oserror_is_unarmed():
    device = FakeDevice(fail_set_timeout=True)
    keeper = arm_watchdog(device=device, monotonic=ManualMonotonic())
    assert keeper.summary == "unarmed reason=set_timeout"
    assert device.keepalive_calls == 0


# --- S0-AC2b: the masking rule ----------------------------------------------

@pytest.mark.parametrize("timeout,expected", [(120, 8), (124, 12), (30, 14), (300, 12), (210, 2)])
def test_masked_hardware_seconds_spot_rows(timeout, expected):
    assert masked_hardware_seconds(timeout) == expected


def test_masked_hardware_seconds_clears_the_floor_for_every_declared_timeout():
    for timeout in WATCHDOG_TIMEOUTS:
        assert masked_hardware_seconds(timeout) >= MIN_MASKED_SECONDS


def test_handover_dropin_renders_exactly_the_two_constants():
    assert handover_dropin() == (
        f"[Manager]\nRuntimeWatchdogSec={RUNTIME_WATCHDOG_SECONDS}s\n"
        f"RebootWatchdogSec={REBOOT_WATCHDOG_SECONDS}s\n"
    ).encode("ascii")


# --- S0-AC3, S0-AC4: budget and pacing -------------------------------------

def test_pet_after_budget_makes_no_further_keepalive_call():
    device = FakeDevice()
    clock = ManualMonotonic()
    keeper = arm_watchdog(device=device, budget=10, monotonic=clock)
    armed_pets = device.keepalive_calls
    clock.advance(10)
    keeper.pet()
    assert device.keepalive_calls == armed_pets


def test_pet_before_budget_still_pets():
    device = FakeDevice()
    clock = ManualMonotonic()
    keeper = arm_watchdog(device=device, budget=10, monotonic=clock)
    armed_pets = device.keepalive_calls
    clock.advance(9)
    keeper.pet()
    assert device.keepalive_calls == armed_pets + 1


def test_paced_yields_every_block_unchanged_and_pets_once_per_block():
    device = FakeDevice()
    keeper = arm_watchdog(device=device, monotonic=ManualMonotonic())
    before = device.keepalive_calls
    blocks = [b"a", b"bb", b"ccc"]
    assert list(keeper.paced(iter(blocks))) == blocks
    assert device.keepalive_calls == before + len(blocks)


# --- S0-AC5: hand_over -------------------------------------------------

def test_hand_over_writes_dropin_mode_0644_under_umask_077_then_pets_then_releases(tmp_path):
    dropin = tmp_path / "sub" / "dropin.conf"
    old_umask = os.umask(0o077)
    try:
        device = FakeDevice()
        keeper = arm_watchdog(device=device, dropin=dropin, monotonic=ManualMonotonic())
        before = device.keepalive_calls
        keeper.hand_over()
    finally:
        os.umask(old_umask)
    assert dropin.read_bytes() == handover_dropin()
    assert stat.S_IMODE(dropin.stat().st_mode) == 0o644
    assert device.keepalive_calls == before + 1
    assert device.released is True


def test_hand_over_with_no_device_still_writes_the_dropin(tmp_path):
    dropin = tmp_path / "dropin.conf"
    keeper = arm_watchdog(device=None, dropin=dropin, monotonic=ManualMonotonic())
    keeper.hand_over()
    assert dropin.read_bytes() == handover_dropin()


def test_real_watchdog_device_never_writes_v_or_issues_wdios_disablecard():
    """S0-AC5: an AST test over bootstrap.py -- the real device class has no
    method that writes 'V' or issues WDIOS_DISABLECARD. Walks actual code
    (Name references, string constants), not the docstring's prose."""
    tree = ast.parse(Path("appliance/bootstrap.py").read_text())
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "WDIOS_DISABLECARD" not in names
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value in ("V", b"V"):
            pytest.fail("bootstrap.py must never write the magic 'V' watchdog byte")


# --- S0-AC11: kernel liveness cmdline parameters ---------------------------

def test_missing_kernel_liveness_both_present():
    values = {path: value for path, value, _ in KERNEL_LIVENESS}
    assert missing_kernel_liveness(read=values.get) == []


def test_missing_kernel_liveness_names_a_wrong_value():
    values = {path: value for path, value, _ in KERNEL_LIVENESS}
    stop_path, _, stop_param = KERNEL_LIVENESS[0]
    values[stop_path] = "1"  # the "off" value, not "0"
    assert missing_kernel_liveness(read=values.get) == [stop_param]


def test_missing_kernel_liveness_names_an_unreadable_path():
    hung_param = KERNEL_LIVENESS[1][2]
    # only the first path's value is given -> read() returns None for the second
    values = {KERNEL_LIVENESS[0][0]: KERNEL_LIVENESS[0][1]}
    assert missing_kernel_liveness(read=values.get) == [hung_param]


def _cmdline_template_command_line(text: str) -> str:
    """The single kernel command line written into cmdline.txt's heredoc
    (S0-AC11: "reads the cmdline template out of build_netboot_bundle.sh"),
    not the whole script -- an adjacent explanatory comment must not be able
    to keep this test green after a regression in the template line itself."""
    match = re.search(
        r'cat > "\$boot_dir/cmdline\.txt" <<\'EOF\'\n(.*?)\nEOF\n',
        text,
        re.DOTALL,
    )
    assert match, "cmdline.txt heredoc not found in build_netboot_bundle.sh"
    body_lines = [line for line in match.group(1).splitlines() if not line.startswith("#")]
    assert len(body_lines) == 1, f"cmdline.txt template must reduce to one command line, got: {body_lines}"
    return body_lines[0]


def test_cmdline_template_carries_every_kernel_liveness_parameter():
    text = Path("scripts/build_netboot_bundle.sh").read_text()
    command_line = _cmdline_template_command_line(text)
    for _path, _value, param in KERNEL_LIVENESS:
        assert param in command_line


# --- S0-AC7: the timeout/budget rule ---------------------------------------

def test_stage1_budget_covers_the_sum_of_stage1_bounds():
    assert STAGE1_BUDGET >= (NETWORKING_TIMEOUT_SECONDS + GATE_BUDGET + LOCATE_DEADLINE
                             + BASE_FETCH_SECONDS + DEBUG_PAUSE_SECONDS)


def test_stage1_watchdog_timeout_covers_the_largest_inter_pet_wait():
    # Between two pets: networking; the clock gate (phase 3 -> 4 lines); a whole locate, taken
    # as if no hop line came; one base-block read (DirectFetch: min(READ_TIMEOUT, remaining));
    # the debug pause after the FAILED line.
    largest_wait = max(NETWORKING_TIMEOUT_SECONDS, GATE_BUDGET, LOCATE_DEADLINE, READ_TIMEOUT,
                       DEBUG_PAUSE_SECONDS)
    assert STAGE1_WATCHDOG_TIMEOUT >= largest_wait + 16


# --- S0-AC10: the provisioning unit's start-limit keys; watchdog.conf gone --

def test_watchdog_conf_no_longer_exists():
    assert not Path("appliance/systemd/watchdog.conf").exists()


def _parse_unit(text: str) -> dict[str, dict[str, list[str]]]:
    """A minimal systemd-unit parser: `[Section]` headers, repeatable
    `key=value` lines (last-standing values are all kept, in order)."""
    sections: dict[str, dict[str, list[str]]] = {}
    section: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            sections.setdefault(section, {})
            continue
        key, sep, value = line.partition("=")
        if sep and section is not None:
            sections[section].setdefault(key.strip(), []).append(value.strip())
    return sections


def test_provision_unit_parses_with_the_three_start_limit_keys():
    path = Path("appliance/systemd/photo-wall-provision.service")
    if shutil.which("systemd-analyze"):
        result = subprocess.run(["systemd-analyze", "verify", str(path)],
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        return
    unit = _parse_unit(path.read_text()).get("Unit", {})
    assert unit.get("StartLimitIntervalSec") == ["10min"]
    assert unit.get("StartLimitBurst") == ["10"]
    assert unit.get("StartLimitAction") == ["reboot-force"]
