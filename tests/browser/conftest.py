"""Bounded public evidence for the opt-in, disposable operator browser checks."""

import importlib.metadata
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

CHECKS = {
    "test_authenticated_operator_registry_walkthrough_and_persistence": (
        "authentication_rejection", "authentication_success", "two_players_three_outputs",
        "frame_creation_binding", "calibration_preview_revert_commit",
        "equipment_replacement_retirement", "fresh_server_persistence", "volatile_browser_token",
    ),
    "test_browser_recovers_after_operator_token_is_rejected": ("authentication_recovery",),
    "test_browser_stale_calibration_conflict_refresh_and_preview_expiry": (
        "stale_calibration_conflict", "inventory_refresh_recovery", "preview_expiry",
    ),
    "test_browser_same_token_reconnect_fences_delayed_rejection": (
        "same_token_reconnect", "delayed_rejection_fence", "current_generation_content_refresh",
    ),
}
RESULTS = pytest.StashKey[dict]()
BROWSERS = pytest.StashKey[set]()
ERRORS = pytest.StashKey[list]()


@pytest.fixture(autouse=True)
def page_errors(context, request):
    """Every page, including additional tabs, must remain free of JS exceptions."""
    errors = []
    request.node.stash[ERRORS] = errors

    def watch(page):
        page.on("pageerror", lambda error: errors.append(type(error).__name__))

    for page in context.pages:
        watch(page)
    context.on("page", watch)
    browser = context.browser
    request.config.stash.setdefault(BROWSERS, set()).add((
        browser.browser_type.name, browser.version,
    ))
    yield
    assert not errors, f"unexpected JavaScript page errors: {len(errors)}"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    name = getattr(item, "originalname", item.name)
    if name not in CHECKS:
        return
    phase = outcome.get_result()
    result = item.config.stash.setdefault(RESULTS, {}).setdefault(name, {"phases": {}})
    result["phases"][phase.when] = phase.outcome
    result["page_errors"] = len(item.stash.get(ERRORS, []))


def source_identity(root):
    """Allowlist provenance; never export environment values or Git status paths."""
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, timeout=5,
    ).strip()
    assert re.fullmatch(r"[a-f0-9]{40}", revision)
    dirty = bool(subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=root, timeout=5,
    ).strip())
    event = os.environ.get("GITHUB_EVENT_NAME", "local")
    if event not in {"local", "push", "pull_request", "workflow_dispatch"}:
        event = "other"
    declared = os.environ.get("GITHUB_SHA", "")
    declared = declared if re.fullmatch(r"[a-f0-9]{40}", declared) else None
    kind = {"pull_request": "synthetic_merge", "workflow_dispatch": "dispatched_ref",
            "push": "pushed_commit"}.get(event, "local_working_tree")
    return {"revision": revision, "dirty": dirty, "event": event,
            "github_sha": declared, "checkout_kind": kind,
            "declared_sha_matches": declared == revision if declared else None}


def pytest_sessionfinish(session, exitstatus):
    if os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1":
        return
    results = session.config.stash.get(RESULTS, {})
    checks = []
    for name, assertions in CHECKS.items():
        result = results.get(name, {})
        phases = result.get("phases", {})
        passed = all(phases.get(phase) == "passed" for phase in ("setup", "call", "teardown"))
        status = "passed" if passed else "failed" if "failed" in phases.values() else "not_completed"
        checks.append({"test": name, "status": status, "assertions": list(assertions),
                       "page_errors": result.get("page_errors", 0)})
    completed = all(check["status"] == "passed" for check in checks)
    document = {
        "schema": 1, "scope": "operator_registry_browser",
        "status": "passed" if completed and exitstatus == 0 else "failed" if exitstatus else "incomplete",
        "recorded_at": datetime.now(UTC).isoformat(),
        "source": source_identity(session.config.rootpath),
        "playwright": importlib.metadata.version("playwright"),
        "browsers": [{"name": name[:24], "version": version[:80]}
                     for name, version in sorted(session.config.stash.get(BROWSERS, set()))],
        "environment": {"database": "real_postgresql_disposable_schema", "transport": "loopback_http",
                        "equipment": "simulated", "players": 2, "outputs": 3,
                        "scheduler": False, "worker": False, "preview_clock": "controlled"},
        "checks": checks,
        "qualification": {"authored_media": False, "scheduled_playback": False,
                          "native_rendering": False, "pxe": False, "physical_outputs": False},
    }
    data = (json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    assert len(data) <= 32768, "browser evidence limit"
    output = Path(session.config.getoption("output"))
    output.mkdir(parents=True, exist_ok=True)
    (output / "operator-browser.json").write_bytes(data)
