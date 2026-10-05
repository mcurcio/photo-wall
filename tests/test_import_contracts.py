"""The node import contracts in pyproject.toml are a ratchet: exemptions only shrink.

lint-imports proves the contracts hold; this test proves nobody widened them to make it pass.
"""

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

SESSION_CONTRACT = "Central session only in Authority and the two exemptions"
LAYERS_CONTRACT = "Node contexts point down"

# The six exemptions frozen by the player-health module design (r8). Removing a line is the
# ratchet turning; adding one is a design change, not a lint fix.
FROZEN_SESSION_EXEMPTIONS = frozenset({
    "appliance.node.host_runner -> appliance.central_session.*",
    "appliance.node.manager_desired -> appliance.central_session.*",
    "appliance.display_host.service -> appliance.central_session.*",  # M2-13
    "appliance.node.broker_runner -> appliance.central_session.*",  # M2-15
    "appliance.node.app_link -> appliance.central_session.*",  # M2-15
    "appliance.node.online_broker -> appliance.central_session.*",  # M2-15
})

# Each forbidden contract's frozen sources and targets. A contract may gain modules (that only
# tightens it, e.g. B9 adds appliance.health as a session source); dropping one fails.
FROZEN_FORBIDDEN_CONTRACTS = {
    "Display never reads the fault catalogue": (
        {"appliance.display_host"},
        {"contracts.node_faults"},
    ),
    SESSION_CONTRACT: (
        {"appliance.display_host", "appliance.node", "appliance.health"},
        {"appliance.central_session"},
    ),
    "Shared node kernel knows no context": (
        {"appliance.clock", "appliance.boot_store", "appliance.feed",
         "appliance.central_session", "appliance.unix_credentials"},
        {"appliance.authority", "appliance.health", "appliance.display_host", "appliance.node"},
    ),
}

FROZEN_NODE_LAYERS = [
    "(appliance.authority)",
    "appliance.health",
    "appliance.display_host | appliance.node",
]


def _contract(name: str) -> dict:
    contracts = tomllib.loads(PYPROJECT.read_text())["tool"]["importlinter"]["contracts"]
    matches = [contract for contract in contracts if contract["name"] == name]
    assert len(matches) == 1, f"expected exactly one import contract named {name!r}"
    return matches[0]


def test_central_session_exemptions_only_shrink():
    contract = _contract(SESSION_CONTRACT)
    assert contract["type"] == "forbidden"
    added = set(contract.get("ignore_imports", ())) - FROZEN_SESSION_EXEMPTIONS
    assert not added, f"new Central-session exemptions are not allowed: {sorted(added)}"


def test_node_layers_are_the_frozen_list():
    contract = _contract(LAYERS_CONTRACT)
    assert contract["type"] == "layers"
    assert contract["layers"] == FROZEN_NODE_LAYERS


def test_forbidden_contracts_keep_every_frozen_source_and_target():
    for name, (sources, targets) in FROZEN_FORBIDDEN_CONTRACTS.items():
        contract = _contract(name)
        assert contract["type"] == "forbidden"
        dropped_sources = sources - set(contract["source_modules"])
        dropped_targets = targets - set(contract["forbidden_modules"])
        assert not dropped_sources, f"{name}: sources dropped {sorted(dropped_sources)}"
        assert not dropped_targets, f"{name}: forbidden modules dropped {sorted(dropped_targets)}"
