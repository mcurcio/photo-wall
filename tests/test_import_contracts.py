"""The import contracts in pyproject.toml are a two-way ratchet: exemptions and retiring code only
shrink; layers stay exact; forbidden contracts only grow.

lint-imports proves the contracts hold; this test proves nobody loosened them to make it pass.
"""

import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import grimp

from scripts.module_closure import first_party_packages

REPO = Path(__file__).resolve().parents[1]
PYPROJECT = REPO / "pyproject.toml"

LAYERS_CONTRACT: Final = "Node contexts point down"
SESSION_CONTRACT: Final = "The Central session is retiring: only the listed workers import it"
NATS_CONTRACT: Final = "Only the Node API library talks to NATS"
FEED_CONTRACT: Final = "The feed is retiring: it gains no importer"
NODE_API_PACKAGE: Final = "nodeapi"
# The Node kernel's whole allow-list besides the stdlib (AGENTS.md code map).
KERNEL: Final = "appliance.kernel"
KERNEL_MAY_IMPORT: Final = ("contracts", "uplink", KERNEL)

# One directory per Node context, in import order (node redesign r3 §3.4). Exact: a change is a
# design change, not a lint fix.
FROZEN_NODE_LAYERS: Final[list[str]] = [
    "node",
    "boot : netboot_init : bootstrap : boot_offer : central_post : node_boot_handoff",
    "apps",
    "health",
    "display_host | host",
    "central_session",
    "kernel : feed : feed_socket",
]

# The retiring feed's importers when the fence went up (r3 §16.1 B2b/B10a). Each line leaves
# with its importer; adding one is a design change, not a lint fix.
FROZEN_FEED_EXEMPTIONS: Final[frozenset[str]] = frozenset({
    "appliance.display_host.runner -> appliance.feed",
    "appliance.display_host.runner -> appliance.feed_socket",
    "appliance.health.judge -> appliance.feed",
    "appliance.health.runner -> appliance.feed",
    "appliance.health.runner -> appliance.feed_socket",
    "appliance.node.app_link -> appliance.feed",
    "appliance.apps.broker_runner -> appliance.feed",
    "appliance.apps.broker_runner -> appliance.feed_socket",
    "appliance.apps.probe_channel -> appliance.feed",
})

# Every contract's ignore lines when E1 froze them. A contract absent here may have none. Removing
# a line is the ratchet turning; adding one fails.
FROZEN_EXEMPTIONS: Final[Mapping[str, frozenset[str]]] = {
    LAYERS_CONTRACT: frozenset({
        "appliance.apps.broker_runner -> appliance.node.app_link",
        "appliance.apps.broker_runner -> appliance.node.recovery_linux",
        "appliance.apps.online_broker -> appliance.node.recovery",
        "appliance.apps.stop_linux -> appliance.node.recovery",
        "appliance.apps.lifecycle_storage -> appliance.node.manager",
        "appliance.host.host_runner -> appliance.node.recovery",
        "appliance.host.host_runner -> appliance.node.recovery_linux",
        "appliance.boot.node_bootstrap -> appliance.node.preparer",
    }),
    "Boot is an island": frozenset({
        "appliance.boot.node_bootstrap -> appliance.apps.environment",
        "appliance.node.preparer -> appliance.apps.environment",
    }),
    SESSION_CONTRACT: frozenset({
        "appliance.host.host_runner -> appliance.central_session.*",
        "appliance.node.manager_desired -> appliance.central_session.*",
        "appliance.display_host.service -> appliance.central_session.*",
        "appliance.apps.broker_runner -> appliance.central_session.*",
        "appliance.node.app_link -> appliance.central_session.*",
        "appliance.apps.online_broker -> appliance.central_session.*",
    }),
    "The base never imports the guest": frozenset({
        "appliance.os_agent -> player.mdns_discovery",
        "appliance.provision -> player.mdns_discovery",
    }),
    "Node contexts never reach V1": frozenset({
        "appliance.process_identity -> appliance.app_launcher",
    }),
    FEED_CONTRACT: FROZEN_FEED_EXEMPTIONS,
}

# The V1 and stage-1 modules the exhaustive layers contract may leave unlayered. Only shrinks.
FROZEN_EXHAUSTIVE_IGNORES: Final[frozenset[str]] = frozenset({
    "app_evidence", "app_executor", "app_launcher", "app_payload", "app_process_proof",
    "app_proof_service", "linux_app_proof", "online_activation", "os_agent", "provision",
    "process_identity",
})

# Each forbidden contract's frozen sources and targets. A contract may gain modules (that only
# tightens it); dropping one fails.
FROZEN_FORBIDDEN_CONTRACTS: Final[Mapping[str, tuple[frozenset[str], frozenset[str]]]] = {
    "Boot is an island": (
        frozenset({"appliance.boot", "appliance.netboot_init", "appliance.bootstrap",
                   "appliance.boot_offer", "appliance.central_post", "appliance.node_boot_handoff"}),
        frozenset({"appliance.apps", "appliance.health", "appliance.display_host", "appliance.host",
                   "appliance.central_session"}),
    ),
    "App lifecycle reaches no sibling context": (
        frozenset({"appliance.apps"}),
        frozenset({"appliance.health", "appliance.display_host", "appliance.host"}),
    ),
    "Health reaches only Display among contexts": (
        frozenset({"appliance.health"}),
        frozenset({"appliance.host"}),
    ),
    "Display never reads the fault catalogue": (
        frozenset({"appliance.display_host"}),
        frozenset({"contracts.node_faults"}),
    ),
    "The base never imports the guest": (
        frozenset({"appliance"}),
        frozenset({"player"}),
    ),
    "Node contexts never reach V1": (
        frozenset({"appliance.kernel", "appliance.feed", "appliance.feed_socket",
                   "appliance.central_session", "appliance.host", "appliance.display_host",
                   "appliance.health", "appliance.apps", "appliance.boot", "appliance.process_identity",
                   "appliance.netboot_init", "appliance.bootstrap", "appliance.boot_offer",
                   "appliance.central_post", "appliance.node_boot_handoff"}),
        frozenset({"appliance.app_evidence", "appliance.app_executor", "appliance.app_launcher",
                   "appliance.app_payload", "appliance.app_process_proof",
                   "appliance.app_proof_service", "appliance.linux_app_proof",
                   "appliance.online_activation", "appliance.os_agent", "appliance.provision"}),
    ),
}

# The retiring packages (r3 §10): their files only leave; new code goes in a context.
RETIRING_FILES: Final[frozenset[str]] = frozenset({
    *(f"appliance/node/{name}.py" for name in (
        "__init__", "app_link", "manager", "manager_desired", "manager_launcher",
        "manager_observation", "manager_runner", "preparer", "recovery", "recovery_linux")),
    *(f"appliance/central_session/{name}.py" for name in ("__init__", "http", "session")),
})


def _importlinter() -> dict:
    return tomllib.loads(PYPROJECT.read_text())["tool"]["importlinter"]


def _contract(name: str) -> dict:
    contracts = _importlinter()["contracts"]
    matches = [contract for contract in contracts if contract["name"] == name]
    assert len(matches) == 1, f"expected exactly one import contract named {name!r}"
    return matches[0]


def test_exemptions_only_shrink() -> None:
    for contract in _importlinter()["contracts"]:
        added = (set(contract.get("ignore_imports", ()))
                 - FROZEN_EXEMPTIONS.get(contract["name"], frozenset()))
        assert not added, f"{contract['name']}: new exemptions are not allowed: {sorted(added)}"
    added = set(_contract(LAYERS_CONTRACT).get("exhaustive_ignores", ())) - FROZEN_EXHAUSTIVE_IGNORES
    assert not added, f"new unlayered appliance modules are not allowed: {sorted(added)}"
    session = _contract(SESSION_CONTRACT)
    assert session["type"] == "protected"
    assert session["protected_modules"] == ["appliance.central_session"]
    assert session["allowed_importers"] == ["appliance.central_session"]


def test_node_layers_are_the_frozen_list() -> None:
    contract = _contract(LAYERS_CONTRACT)
    assert contract["type"] == "layers"
    assert contract["containers"] == ["appliance"]
    assert contract["exhaustive"] is True
    assert contract["layers"] == FROZEN_NODE_LAYERS


def test_forbidden_contracts_keep_every_frozen_source_and_target() -> None:
    for name, (sources, targets) in FROZEN_FORBIDDEN_CONTRACTS.items():
        contract = _contract(name)
        assert contract["type"] == "forbidden"
        dropped_sources = sources - set(contract["source_modules"])
        dropped_targets = targets - set(contract["forbidden_modules"])
        assert not dropped_sources, f"{name}: sources dropped {sorted(dropped_sources)}"
        assert not dropped_targets, f"{name}: forbidden modules dropped {sorted(dropped_targets)}"


def test_no_node_contract_silences_unmatched_exemptions() -> None:
    silenced = [contract["name"] for contract in _importlinter()["contracts"]
                if "unmatched_ignore_imports_alerting" in contract]
    assert not silenced, f"unmatched exemptions must stay loud: {silenced}"


def test_retiring_packages_only_shrink() -> None:
    files = {path.relative_to(REPO).as_posix()
             for package in ("appliance/node", "appliance/central_session")
             for path in (REPO / package).glob("*.py")}
    added = files - RETIRING_FILES
    assert not added, f"new code in a retiring package is not allowed: {sorted(added)}"


def test_root_packages_are_every_first_party_package() -> None:
    assert set(_importlinter()["root_packages"]) == set(first_party_packages(REPO))


def test_the_nats_fence_covers_every_root_package_but_the_node_api() -> None:
    contract = _contract(NATS_CONTRACT)
    assert contract["type"] == "forbidden"
    assert set(contract["source_modules"]) == (
        set(_importlinter()["root_packages"]) - {NODE_API_PACKAGE})
    assert contract["forbidden_modules"] == ["nats"]


def test_feed_exemptions_only_shrink() -> None:
    contract = _contract(FEED_CONTRACT)
    assert contract["type"] == "protected"
    assert set(contract["protected_modules"]) == {"appliance.feed", "appliance.feed_socket"}
    added = set(contract.get("ignore_imports", ())) - FROZEN_FEED_EXEMPTIONS
    assert not added, f"new feed importers are not allowed: {sorted(added)}"


def test_the_node_kernel_imports_only_the_stdlib_contracts_and_uplink() -> None:
    """An allow-list, not a denylist: a package first imported tomorrow fails here too."""
    graph = grimp.build_graph(*_importlinter()["root_packages"], include_external_packages=True)
    reached = graph.find_upstream_modules(KERNEL, as_package=True)
    foreign = sorted(module for module in reached
                     if module.split(".")[0] not in sys.stdlib_module_names
                     and not any(module == allowed or module.startswith(allowed + ".")
                                 for allowed in KERNEL_MAY_IMPORT))
    assert not foreign, f"{KERNEL} reaches beyond the stdlib, contracts and uplink: {foreign}"
