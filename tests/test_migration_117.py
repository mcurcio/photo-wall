"""117 (decision 0034, slice S2a): the dead tables are gone and no code names them; no
immutability trigger and no Central function is left, but the device lifecycle's (S2e drops it);
an admission's offer is the boot offer itself.

Mutation probes: (1) drop one `DROP TRIGGER` line from 117 -> that trigger is still listed;
(2) name `node_offer_contexts` in `central/fleet/node_boot.py` -> the source scan fails.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
DROPPED = ("node_ci_publication_heads", "node_ci_publications", "node_ci_revoked_qualifications",
           "node_offer_contexts", "node_release_verifications", "media_orphans", "audit_events",
           "runtime_changes")
# S2e drops the device lifecycle's trigger and function with its merge into `devices`. Procrastinate's
# own (`procrastinate_*`, the job queue library's schema) are not Central's.
LIBRARY = "procrastinate_"
UNTIL_S2E_TRIGGERS: set[tuple[str, str]] = set()   # S2e (121) dropped the last, the device lifecycle's
UNTIL_S2E_FUNCTIONS: set[str] = set()
SCANNED = ("central", "media", "scripts")
SOURCE = {".py", ".js", ".jsx", ".ts", ".tsx", ".sql", ".sh"}
SKIPPED = {"migrations", "node_modules", "dist", "__pycache__"}


@pytest.mark.db
def test_117_leaves_no_immutability_trigger_and_no_central_function(registry):
    with registry.db.transaction() as conn:
        triggers = {(row["event_object_table"], row["trigger_name"]) for row in conn.execute(
            "SELECT event_object_table, trigger_name FROM information_schema.triggers "
            "WHERE trigger_schema='public'").fetchall()
            if not row["event_object_table"].startswith(LIBRARY)}
        functions = {row["proname"] for row in conn.execute(
            "SELECT proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='public'").fetchall() if not row["proname"].startswith(LIBRARY)}
    assert triggers == UNTIL_S2E_TRIGGERS
    assert functions == UNTIL_S2E_FUNCTIONS


@pytest.mark.db
def test_117_drops_the_dead_tables(registry):
    """(117 also pointed a boot admission at its boot offer; 128 drops the admissions.)"""
    with registry.db.transaction() as conn:
        for name in DROPPED:
            assert conn.execute("SELECT to_regclass(%s) AS t", (name,)).fetchone()["t"] is None, name


def _sources():
    for top in SCANNED:
        for path in (ROOT / top).rglob("*"):
            if path.suffix in SOURCE and path.is_file() and not SKIPPED & set(path.parts):
                yield path


def test_no_code_names_a_dropped_table():
    pattern = re.compile(r"\b(" + "|".join(DROPPED) + r")\b")
    named = sorted(f"{path.relative_to(ROOT)}:{number}"
                   for path in _sources()
                   for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1)
                   if pattern.search(line))
    assert named == []
