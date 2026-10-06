"""E1-2 module map: the single input to the rewrite, the closure check and the residual grep.

Run tool, unshipped (`.claude/**`). The table is the frozen page of bead E1-2
(`.claude/runs/e1-layered-codebase.md`, "Module map"): 25 moves; the probe split and the
four package `__init__` files are made by `rewrite.py`.
"""
from __future__ import annotations

from typing import Final

_KERNEL: Final = (
    ("appliance.clock", "appliance.kernel.clock"),
    ("appliance.boot_store", "appliance.kernel.boot_store"),
    ("appliance.unix_credentials", "appliance.kernel.unix_credentials"),
    ("appliance.node.capacity", "appliance.kernel.capacity"),
    ("appliance.node.boot_stage", "appliance.kernel.boot_stage"),
)
_HOST: Final = tuple((f"appliance.node.{name}", f"appliance.host.{name}") for name in (
    "host", "host_linux", "host_runner", "host_storage", "base_status"))
_BOOT: Final = (
    ("appliance.node.bootstrap", "appliance.boot.node_bootstrap"),  # the one rename: stage 1 owns `bootstrap`
    ("appliance.node.storage_mount", "appliance.boot.storage_mount"),
)
_APPS: Final = tuple((f"appliance.node.{name}", f"appliance.apps.{name}") for name in (
    "broker", "broker_runner", "online_broker", "online_runner", "import_worker", "root_import",
    "environment", "process_linux", "stop_linux", "stop_operation", "lifecycle_storage", "probe",
    "probe_channel"))

MOVES: Final[tuple[tuple[str, str], ...]] = (*_KERNEL, *_HOST, *_BOOT, *_APPS)

PACKAGES: Final[dict[str, str]] = {
    "appliance.kernel": "Kernel: the Node's stdlib-only shared primitives (clock, boot store, credentials, capacity, stage records, probe timing).",
    "appliance.host": "Host: the base-owned host core (metrics, facts, reboot and local recovery).",
    "appliance.boot": "Boot: the Node's one-shot boot stages (storage, handoff, prepare).",
    "appliance.apps": "Apps: the app effect broker, its probe and the app process adapters.",
}

# The probe split: names that leave `appliance.node.probe` for `appliance.kernel.probe_timing`.
PROBE_TIMING: Final = "appliance.kernel.probe_timing"
PROBE_TIMING_NAMES: Final = ("PROBE_PERIOD_MS", "MISS_LIMIT", "STARTUP_BUDGET_MS", "KILL_AFTER_MS",
                             "OUTSTANDING_LIMIT", "ProbeTiming", "SHIPPED_TIMING")


def renamed(module: str) -> str:
    """`module` through the map: longest dotted-prefix match; identity if unmoved."""
    best: tuple[str, str] | None = None
    for old, new in MOVES:
        if (module == old or module.startswith(old + ".")) and (best is None or len(old) > len(best[0])):
            best = (old, new)
    return module if best is None else best[1] + module[len(best[0]):]
