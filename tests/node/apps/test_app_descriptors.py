"""The broker's open-file measure of the app run, read from /proc from outside (1b P1b)."""
import os
import resource
import sys
from pathlib import Path

import pytest

from appliance.apps.descriptors import DescriptorUse, descriptor_use, parse_soft_limit
from appliance.process_identity import read_proc_start_ticks

# A /proc/<pid>/limits text as Linux writes it (fs/proc/base.c, "%-25s %-20s %-20s %-10s\n";
# the values of a Raspberry Pi OS Player: soft 1,024 open files).
ROWS = (
    ("Limit", "Soft Limit", "Hard Limit", "Units"),
    ("Max cpu time", "unlimited", "unlimited", "seconds"),
    ("Max file size", "unlimited", "unlimited", "bytes"),
    ("Max data size", "unlimited", "unlimited", "bytes"),
    ("Max stack size", "8388608", "unlimited", "bytes"),
    ("Max core file size", "0", "unlimited", "bytes"),
    ("Max resident set", "unlimited", "unlimited", "bytes"),
    ("Max processes", "31529", "31529", "processes"),
    ("Max open files", "1024", "524288", "files"),
    ("Max locked memory", "8388608", "8388608", "bytes"),
    ("Max address space", "unlimited", "unlimited", "bytes"),
    ("Max file locks", "unlimited", "unlimited", "locks"),
    ("Max pending signals", "31529", "31529", "signals"),
    ("Max msgqueue size", "819200", "819200", "bytes"),
    ("Max nice priority", "0", "0", ""),
    ("Max realtime priority", "0", "0", ""),
    ("Max realtime timeout", "unlimited", "unlimited", "us"),
)


def row(*fields: str) -> str:
    return "%-25s %-20s %-20s %-10s\n" % fields


LIMITS = "".join(row(*fields) for fields in ROWS)
OPEN_FILES = row("Max open files", "1024", "524288", "files")


def proc_tree(root: Path, pid: int, ticks: int, *, open_files: int, limits: str = LIMITS) -> Path:
    """A fake /proc with one process: its stat (kernel birth), fd entries and limits."""
    process = root / str(pid)
    (process / "fd").mkdir(parents=True)
    (process / "stat").write_text(f"{pid} (player) " + " ".join(["S"] + ["0"] * 18 + [str(ticks)]))
    for number in range(open_files):
        (process / "fd" / str(number)).write_text("")
    (process / "limits").write_text(limits)
    return root


def test_the_soft_limit_is_read_from_the_max_open_files_row():
    assert parse_soft_limit(LIMITS) == 1024


@pytest.mark.parametrize("limits", [
    LIMITS.replace(OPEN_FILES, ""),
    LIMITS + OPEN_FILES,
    LIMITS.replace(OPEN_FILES, row("Max open files", "unlimited", "unlimited", "files")),
], ids=["missing", "doubled", "unlimited"])
def test_a_missing_doubled_or_unlimited_row_is_unreadable(limits):
    with pytest.raises(ValueError, match="^limits_unreadable$"):
        parse_soft_limit(limits)


def test_descriptor_use_counts_the_fd_entries_of_the_same_process(tmp_path):
    proc = proc_tree(tmp_path, 4242, 31751781, open_files=7)
    assert descriptor_use(proc, 4242, 31751781) == DescriptorUse(7, 1024)


def test_a_reused_pid_or_a_gone_process_is_never_measured(tmp_path):
    proc = proc_tree(tmp_path, 4242, 31751781, open_files=7)
    assert descriptor_use(proc, 4242, 31751780) is None  # the pid now names another process
    assert descriptor_use(proc, 4343, 1) is None  # gone


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads the real /proc (Linux)")
def test_the_real_proc_of_this_process_is_measured():
    proc, pid = Path("/proc"), os.getpid()
    use = descriptor_use(proc, pid, read_proc_start_ticks(proc, pid))
    assert use is not None and use.open >= 3
    assert use.soft_limit == resource.getrlimit(resource.RLIMIT_NOFILE)[0]
