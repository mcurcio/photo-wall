#!/usr/bin/env python3
"""Run D17 owner-contract checks in immutable Central images, never source overlays.

Only tests and pytest's own pure-Python dependencies are mounted. The production
interpreter/dependencies/code remain the image's installed /app tree. All DB
schemas are disposable; no deployment or real effect adapter is used.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SUITES = {
    "compatibility_matrix": (
        "test_player_control_protocol.py::test_future_control_schema_offer_selects_highest_known_common",
        "test_player_control_protocol.py::test_legacy_http_and_websocket_state_and_identify_refusal",
        "test_player_control_protocol.py::test_mixed_pod_enrollment_without_control_row_seals_legacy",
        "test_player_control_protocol.py::test_first_state_seals_hello_and_new_epoch_can_negotiate",
        "test_player_control_protocol.py::test_delivery_sequence_and_applied_receipt_remain_distinct_from_latest_result",
        "test_player_control_protocol.py::test_older_central_writer_cannot_leave_a_replayable_receipt",
        "test_published_player_wire.py", "test_fleet_os_routes.py", "test_node_protocol.py", "test_node_boot.py"),
    "fence_contract": ("test_equipment_drain.py", "test_equipment_drain_in.py", "test_node_lifecycle.py", "test_node_runtime_reconciliation.py"),
    "readiness_contract": ("test_readiness_diagnostics.py", "test_coordination.py", "test_node_central.py"),
}
PRODUCTION_MODULES = ("central.app", "central.coordination", "central.fleet.node_lifecycle", "contracts.node_protocol")


def _run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def inside(output: Path):
    # -I ignores host PYTHONPATH/user site. /qualification contains tests, never
    # a production package. Production takes precedence over test tooling.
    sys.path[:0] = ["/app", "/test-deps", "/qualification"]
    imports = {}
    for name in PRODUCTION_MODULES:
        path = Path(importlib.import_module(name).__file__).resolve()
        if str(path) != "/app/"+name.replace(".", "/")+".py":
            raise ValueError("qualification_source_shadowed")
        imports[name] = str(path)
    if sys.executable != "/app/.venv/bin/python":
        raise ValueError("qualification_interpreter_changed")
    import pytest

    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    for category, files in SUITES.items():
        result = pytest.main(["-q", "-p", "no:cacheprovider", "--rootdir=/qualification/tests",
            "--junitxml="+str(output / (category+".xml")),
            *("/qualification/tests/"+name for name in files)])
        if result != 0:
            raise RuntimeError("qualification_suite_failed:"+category)
    for name, module in tuple(sys.modules.items()):
        if name.split(".", 1)[0] in {"central", "contracts", "media", "player"}:
            filename = getattr(module, "__file__", None)
            if filename and not Path(filename).resolve().is_relative_to("/app"):
                raise ValueError("qualification_source_shadowed")
    (output / "execution.json").write_text(json.dumps({"interpreter": sys.executable, "imports": imports}))


def _test_dependencies(destination: Path):
    # Exact tools from the lock-installed CI environment; no application deps
    # are overlaid. Docker Linux and CI host use the same CPython minor version.
    for name in ("pytest", "_pytest", "iniconfig", "packaging", "pluggy", "pygments"):
        module = importlib.import_module(name)
        source = Path(module.__file__).parent
        shutil.copytree(source, destination / name, ignore=shutil.ignore_patterns("__pycache__"))


def check(images: list[str], revision: str, output: Path):
    sys.path.insert(0, str(ROOT))
    from contracts.node_rollout import canonical, image_digest, validate_qualification
    from scripts.node_rollout_ci_evidence import report_result
    from scripts.published_player_wire import prepare

    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("qualification_revision_invalid")
    for reference in images:
        image_digest(reference)
    output.mkdir(parents=True, exist_ok=True)
    (output / "reports").mkdir(exist_ok=True)
    postgres = re.search(r"image: (postgres:[^\s]+@sha256:[0-9a-f]{64})", (ROOT / "compose.yaml").read_text()).group(1)
    identity = "pw-qualification-"+uuid4().hex
    rows = []
    suite_sources = {str(path.relative_to(ROOT)): sha256(path.read_bytes()).hexdigest()
                     for path in (ROOT / "tests").rglob("*.py")}
    suite_sources["harness"] = sha256(Path(__file__).read_bytes()).hexdigest()
    suite_hash = sha256(canonical({"suites": SUITES, "sources": suite_sources})).hexdigest()
    with tempfile.TemporaryDirectory(prefix="pw-image-qualification-") as scratch:
        scratch = Path(scratch)
        _test_dependencies(scratch / "deps")
        prepare(scratch / "published-player-wire")
        (scratch / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts/published_player_wire.py", scratch / "scripts/published_player_wire.py")
        _run("docker", "network", "create", "--internal", identity, stdout=subprocess.DEVNULL)
        try:
            _run("docker", "run", "-d", "--name", identity, "--network", identity,
                "--network-alias", "database", "-e", "POSTGRES_PASSWORD=isolated-test-only",
                "--tmpfs", "/var/lib/postgresql/data", postgres, stdout=subprocess.DEVNULL)
            deadline = time.monotonic()+60
            while subprocess.run(["docker", "exec", identity, "pg_isready", "-U", "postgres"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
                if time.monotonic() >= deadline:
                    raise TimeoutError("qualification_database_unavailable")
                time.sleep(.25)
            for number, reference in enumerate(sorted(set(images))):
                _run("docker", "pull", "--platform", "linux/amd64", reference)
                inspected = json.loads(_run("docker", "image", "inspect", reference, capture_output=True).stdout)[0]
                if reference not in inspected["RepoDigests"] or inspected["Architecture"] != "amd64" or inspected["Os"] != "linux":
                    raise ValueError("qualification_image_identity_changed")
                result = output / str(number)
                result.mkdir(exist_ok=True)
                result.chmod(0o777)  # Dedicated temporary report output, writable by image UID.
                _run("docker", "run", "--rm", "--platform", "linux/amd64", "--read-only",
                    "--network", identity, "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--tmpfs", "/tmp:rw,nosuid,nodev", "--workdir", "/app",
                    "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                    "-e", "PHOTO_WALL_PUBLISHED_PLAYER_WIRE_DIR=/published-player-wire",
                    "-e", "PHOTO_WALL_TEST_DATABASE_URL=postgresql://postgres:isolated-test-only@database/postgres",
                    "-v", f"{ROOT / 'tests'}:/qualification/tests:ro",
                    "-v", f"{scratch / 'scripts'}:/qualification/scripts:ro",
                    "-v", f"{scratch / 'published-player-wire'}:/published-player-wire:ro",
                    "-v", f"{Path(__file__).resolve()}:/qualification/check.py:ro",
                    "-v", f"{scratch / 'deps'}:/test-deps:ro", "-v", f"{result.resolve()}:/reports:rw",
                    "--entrypoint", "/app/.venv/bin/python", reference, "-I", "/qualification/check.py", "--inside")
                execution = json.loads((result / "execution.json").read_text())
                reports = {}
                for category in SUITES:
                    raw = (result / (category+".xml")).read_bytes()
                    reports[category] = report_result(raw)
                    (output / "reports" / (reports[category]["sha256"]+".xml")).write_bytes(raw)
                rows.append({"reference": reference, "image_config_id": inspected["Id"], "platform": "linux/amd64",
                             **execution, "reports": reports})
        finally:
            subprocess.run(["docker", "rm", "-fv", identity], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["docker", "network", "rm", identity], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    now = time.time()
    payload = validate_qualification({"schema": 1, "kind": "image_qualification", "revision": revision,
        "suite_sha256": suite_hash, "created_at": now, "expires_at": now+30*86400, "images": rows})
    (output / "qualification.json").write_bytes(canonical(payload))
    (output / "status.json").write_bytes(canonical({"state": "unsigned_unqualified", "suite_sha256": suite_hash,
        "reason": "separate_trusted_ci_signature_and_live_scope_publication_required"}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inside", action="store_true")
    parser.add_argument("--image", action="append", default=[])
    parser.add_argument("--revision")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.inside:
        inside(Path("/reports"))
    elif args.image and args.revision and args.output:
        check(args.image, args.revision, args.output)
    else:
        parser.error("--image, --revision and --output are required")


if __name__ == "__main__":
    main()
