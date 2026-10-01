"""Actual final live identity witness; never substitute an admitted historical link."""

import json
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

current = json.loads(sys.argv[1])
expected = current["process"]
properties = "ActiveState,SubState,MainPID,InvocationID,RootDirectory,ControlGroup,User,Group"


def observe():
    text = subprocess.check_output(
        ["systemctl", "show", "photo-wall-node-player.service", "-p", properties], text=True
    )
    rows = dict(line.split("=", 1) for line in text.splitlines())
    assert rows["ActiveState"] == "active" and rows["SubState"] == "running", rows
    assert int(rows["MainPID"]) == expected["pid"], rows
    assert str(UUID(rows["InvocationID"])) == expected["invocation_id"], rows
    assert rows["RootDirectory"].endswith("/" + current["environment_sha256"] + "/rootfs"), rows
    raw = (Path("/proc") / str(expected["pid"]) / "stat").read_text()
    fields = raw[raw.rfind(")") + 2 :].split()
    assert fields[0] != "Z" and int(fields[19]) == expected["start_ticks"], fields
    return rows


first = observe()
assert observe() == first
print(
    json.dumps(
        {
            "properties": first,
            "process": expected,
            "boottime_ms": int(time.clock_gettime(time.CLOCK_BOOTTIME) * 1000),
        },
        sort_keys=True,
    )
)
