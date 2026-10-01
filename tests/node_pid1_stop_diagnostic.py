"""Opt-in fixture wrapper: preserve original stop result; log bounded failed samples."""

import collections
import json
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, "/usr/lib/photo-wall-app-broker")
from appliance.node import process_linux
from appliance.node.broker_runner import main

local = threading.local()
original_show = process_linux.systemctl_show
original_stop = process_linux.SystemdAppProcessDriver.stop


def traced_show(unit):
    try:
        rows = original_show(unit)
    except Exception as error:
        if getattr(local, "samples", None) is not None:
            local.samples.append(
                {
                    "boottime_ms": process_linux.boottime_ms(),
                    "show_error": type(error).__name__,
                    "reason": str(error)[:256],
                }
            )
        raise
    if getattr(local, "samples", None) is not None:
        local.samples.append({"boottime_ms": process_linux.boottime_ms(), "rows": rows})
    return rows


def evidence(expected, error, result):
    value = {
        "fixture_diagnostic_only": True,
        "outcome": result,
        "error_type": type(error).__name__ if error else None,
        "reason": str(error)[:256] if error else None,
        "pid": expected.process.pid,
        "invocation_id": str(expected.process.invocation_id),
        "start_ticks": expected.process.start_ticks,
        "samples": list(local.samples),
        "boottime_ms": process_linux.boottime_ms(),
    }
    paths = {
        "stat": Path("/proc") / str(expected.process.pid) / "stat",
        "cgroup": Path("/proc") / str(expected.process.pid) / "cgroup",
        "cgroup_events": Path(
            "/sys/fs/cgroup/photowallapp.slice/photo-wall-node-player.service/cgroup.events"
        ),
        "cgroup_procs": Path(
            "/sys/fs/cgroup/photowallapp.slice/photo-wall-node-player.service/cgroup.procs"
        ),
    }
    for key, path in paths.items():
        try:
            value[key] = path.read_text()[:4096]
        except OSError as exc:
            value[key] = {"errno": exc.errno}
    try:
        value["proc_root"] = os.readlink(Path("/proc") / str(expected.process.pid) / "root")
    except OSError as exc:
        value["proc_root"] = {"errno": exc.errno}
    print("NODE_STOP_DIAGNOSTIC " + json.dumps(value, sort_keys=True), file=sys.stderr, flush=True)


def traced_stop(self, expected, *, expires_boottime_ms):
    local.samples = collections.deque(maxlen=24)
    try:
        result = original_stop(self, expected, expires_boottime_ms=expires_boottime_ms)
    except BaseException as error:
        try:
            evidence(expected, error, "raised")
        except Exception:
            pass  # Diagnostic failure never replaces original behavior.
        raise
    else:
        if not result:
            try:
                evidence(expected, None, "false")
            except Exception:
                pass
        return result
    finally:
        local.samples = None


process_linux.systemctl_show = traced_show
process_linux.SystemdAppProcessDriver.stop = traced_stop
main()
