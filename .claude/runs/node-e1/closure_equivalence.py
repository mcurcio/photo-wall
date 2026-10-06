"""E1-2 closure equivalence: each launcher ships the same code, renamed (AC3).

Run tool, unshipped (`.claude/**`). Prior art: `sim2/allclosures.py`, `sim2/delta.py`.
Usage: python .claude/runs/node-e1/closure_equivalence.py <before-tree> <after-tree>
Exit 0 when `unexpected_deltas` is empty.
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parent))
from module_map import renamed  # noqa: E402

_CLOSURES: Final = """\
import json, sys
sys.path.insert(0, sys.argv[1])
from scripts.module_closure import POLICIES as CORE, closure_for
from scripts.build_node_base_deb import POLICIES as BASE
from scripts.build_node_manager_deb import POLICY as MANAGER
policies = {**CORE, **BASE, "node-manager": MANAGER}
print(json.dumps({name: closure_for(policy, repo=__import__("pathlib").Path(sys.argv[1])).modules
                  for name, policy in policies.items()}))
"""

# The bead's expected table (measured on sim4, old modules mapped through the map): gains, losses.
EXPECTED: Final[Mapping[str, tuple[frozenset[str], frozenset[str]]]] = {
    "root-import": (frozenset({"appliance.apps", "appliance.kernel"}), frozenset({"appliance.node"})),
    "node-bootstrap": (frozenset({"appliance.apps", "appliance.boot", "appliance.kernel"}), frozenset()),
    "display-controller": (frozenset({"appliance.kernel"}), frozenset()),
    "host-core": (frozenset({"appliance.host", "appliance.kernel"}), frozenset()),
    "app-broker": (frozenset({"appliance.apps", "appliance.kernel", "appliance.kernel.probe_timing"}), frozenset()),
    "manager-supervisor": (frozenset({"appliance.apps", "appliance.host", "appliance.kernel"}), frozenset()),
    "health-judge": (frozenset({"appliance.kernel", "appliance.kernel.probe_timing"}),
                     frozenset({"appliance.apps.probe", "appliance.node"})),
    "node-manager": (frozenset({"appliance.apps", "appliance.kernel"}), frozenset()),
    "initrd": (frozenset(), frozenset()),
    "bootstrapper": (frozenset(), frozenset()),
    "player": (frozenset(), frozenset()),
}


def closures(tree: Path) -> Mapping[str, frozenset[str]]:
    """The first-party modules of all 11 launcher policies over `tree`."""
    run = subprocess.run([sys.executable, "-c", _CLOSURES, str(tree.resolve())], cwd=tree,
                         capture_output=True, text=True)
    if run.returncode:
        raise SystemExit(f"closures of {tree} refused: {run.stderr.strip().splitlines()[-1]}")
    out = run.stdout
    return {name: frozenset(modules) for name, modules in json.loads(out).items()}


def unexpected_deltas(before: Mapping[str, frozenset[str]],
                      after: Mapping[str, frozenset[str]]) -> tuple[str, ...]:
    problems: list[str] = []
    for name in sorted(set(before) | set(after) | set(EXPECTED)):
        if name not in before or name not in after or name not in EXPECTED:
            problems.append(f"{name}: launcher missing from before, after or the expected table")
            continue
        mapped = {renamed(module) for module in before[name]}
        gains, losses = frozenset(after[name] - mapped), frozenset(mapped - after[name])
        want_gains, want_losses = EXPECTED[name]
        if gains != want_gains:
            problems.append(f"{name}: gains {sorted(gains)}, expected {sorted(want_gains)}")
        if losses != want_losses:
            problems.append(f"{name}: loses {sorted(losses)}, expected {sorted(want_losses)}")
    return tuple(problems)


def main() -> int:
    problems = unexpected_deltas(closures(Path(sys.argv[1])), closures(Path(sys.argv[2])))
    for problem in problems:
        print(problem)
    print(f"unexpected deltas: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
