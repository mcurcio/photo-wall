"""The Pi's old HTTP path is gone from the code (decision 0034, S4c): no file Central, the Pi or the
build ships names a removed route or a table migration 128 dropped, and the Pi's Central session
package is deleted. Migrations (history) and tests are not scanned. A module named like a table
(`contracts/node_host_facts.py`, the host line's facts) is not the table: a name reached through a
`.` or a `/` is a path, not a table.

Mutation probe: keep one `/v2/node/sessions` reference in central/fleet/node_routes.py -> fails.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCANNED = ("central", "appliance", "player", "contracts", "media", "scripts")
SOURCE = {".py", ".js", ".jsx", ".ts", ".tsx", ".sql", ".sh", ".c", ".h", ".conf", ".service", ".json"}
SKIPPED = {"migrations", "node_modules", "dist", "__pycache__", "storybook-static"}

# Every route the old path served: part 1's command poll, 2a-iii's app operations, 2a-iv's posts
# behind no page and 2a-v's sessions and app links. A path parameter's segment is matched by the
# route's fixed part.
ROUTES = (
    "/v2/node/commands",
    "/v2/node/app-preparation", "/v2/node/app-effects", "/v2/node/app-responses", "/v2/node/app-desired",
    "/v2/node/app-commands", "/v2/node/app-attempts", "/app-stages", "/app-qualifications", "/app-attempts",
    "/v2/node/evidence", "/v2/node/observations", "/v2/node/host-facts", "/v2/node/display",
    "/v2/node/sessions", "/v2/node/app-links",
)
TABLES = (
    "node_reconciliation_work", "node_evidence", "node_display_withdrawals", "node_display_handoffs",
    "node_display_exchanges", "node_reboot_commands", "node_command_responses", "node_app_effects",
    "node_app_responses", "node_app_links", "node_manager_observations", "node_host_observations",
    "node_host_facts", "node_sessions", "node_producers", "node_boot_admissions",
    "node_environment_acceptances", "node_app_qualification_samples", "node_app_qualifications",
    "node_app_operations",
)
_ROUTE = re.compile("(?:" + "|".join(re.escape(route) for route in ROUTES) + r")(?![\w-])")
_TABLE = re.compile(r"(?<![\w./])(?:" + "|".join(TABLES) + r")(?!\w|\.py)")


def _sources():
    for top in SCANNED:
        for path in (ROOT / top).rglob("*"):
            if path.suffix in SOURCE and path.is_file() and not SKIPPED & set(path.parts):
                yield path


def test_no_code_names_a_removed_route_or_a_dropped_table():
    found = []
    for path in _sources():
        for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            for match in (*_ROUTE.finditer(line), *_TABLE.finditer(line)):
                found.append(f"{path.relative_to(ROOT)}:{number}: {match.group(0)}")
    assert found == []


def test_the_pis_central_session_package_is_gone():
    assert not (ROOT / "appliance/central_session").exists()


def test_the_scan_finds_a_route_and_a_table_and_passes_over_a_module_path():
    assert _ROUTE.search('@app.post("/v2/node/sessions")')
    assert _ROUTE.search("POST /v2/node/display")
    assert not _ROUTE.search('"/v2/node/display-host"')
    assert _TABLE.search("SELECT * FROM node_sessions WHERE")
    assert _TABLE.search("JOIN node_sessions s ON s.producer_id=node_producers.producer_id")
    assert not _TABLE.search("from contracts.node_host_facts import BOOT_STAGES")
    assert not _TABLE.search("contracts/node_host_facts.py")
