"""Execute the shipped audit helper without repository or application imports."""

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.harness_bundle import WALL_HELPER_BUNDLE
from scripts.provenance_models import (
    MAX_PROVENANCE_BYTES,
    decode_provenance,
    inventory_sha256,
)
from scripts.runtime_provenance import collect_provenance


@pytest.fixture
def source_trees(tmp_path):
    app = tmp_path / "app"
    harness = tmp_path / "harness"
    files = {}
    for name in ("central/app.py", "central/migrations/001.sql", "media/immich.py",
                 "media/prepare.py", "contracts/models.py", "player/service.py"):
        path = app / name
        path.parent.mkdir(parents=True, exist_ok=True)
        # These are bytes to audit; importing them must never be necessary.
        data = b"synthetic source which is deliberately not executable Python\n" + name.encode()
        path.write_bytes(data)
        files[name] = hashlib.sha256(data).hexdigest()
    (app / "central/unrelated.json").write_text("not source")
    bundle = WALL_HELPER_BUNDLE.stage(Path(__file__).parents[1], harness)
    (harness / "bundle.json").write_text(json.dumps({"schema": 1, "files": bundle}))
    return app, harness, files, bundle


def run_helper(app, harness):
    # Match production's file entry point and /app working directory. Isolated
    # Python ignores checkout/PYTHONPATH, while the normal venv supplies Pydantic.
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    return subprocess.run(
        [sys.executable, "-I", str(harness / "scripts/runtime_provenance.py"),
         "--app-root", str(app), "--harness-root", str(harness)],
        cwd=app, env=env, capture_output=True, text=True, timeout=10,
    )


def test_staged_helper_executes_independently_and_proves_every_source(source_trees):
    app, harness, expected, bundle = source_trees
    process = run_helper(app, harness)
    assert process.returncode == 0, process.stderr
    assert process.stderr == ""
    result = decode_provenance(process.stdout)
    assert result.files == expected
    assert result.core_inventory_sha256 == inventory_sha256(expected)
    assert result.adapter_sha256 == expected["media/immich.py"]
    assert result.preparer_sha256 == expected["media/prepare.py"]
    assert result.harness_sha256 == bundle["demo_wall.py"]
    assert result.helper_bundle.files == bundle
    assert {"scripts/runtime_provenance.py", "scripts/provenance_models.py"} <= set(bundle)


@pytest.mark.parametrize("name", [item.target for item in WALL_HELPER_BUNDLE.files])
def test_collector_hashes_every_bundle_member_including_itself(source_trees, name):
    app, harness, _, _ = source_trees
    path = harness / name
    path.write_bytes(path.read_bytes() + b"\n# changed after manifest publication\n")
    with pytest.raises(ValueError, match="provenance_bundle_mismatch"):
        collect_provenance(app, harness)


@pytest.mark.parametrize("mutation", ["missing", "extra", "symlink", "directory"])
def test_collector_rejects_incomplete_or_unsafe_helper_tree(source_trees, mutation):
    app, harness, _, _ = source_trees
    path = harness / "scripts/immich_actions.py"
    if mutation == "extra":
        (harness / "scripts/undeclared.py").write_text("# extra source\n")
    else:
        path.unlink()
        if mutation == "symlink":
            path.symlink_to(app / "media/immich.py")
        elif mutation == "directory":
            path.mkdir()
    process = run_helper(app, harness)
    assert process.returncode == 1
    assert process.stdout == ""
    assert process.stderr == '{"error":"runtime_provenance_invalid"}\n'


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(extra="private-token-must-not-be-retained"),
    lambda value: value.update(schema=True),
    lambda value: value.update(schema="1"),
    lambda value: value.update(core_inventory_sha256="0" * 64),
    lambda value: value.update(adapter_sha256="0" * 64),
    lambda value: value.update(preparer_sha256="0" * 64),
    lambda value: value.update(harness_sha256="0" * 64),
    lambda value: value["files"].pop("media/immich.py"),
    lambda value: value["files"].update({"/private/source.py": "a" * 64}),
    lambda value: value["files"].update({"../source.py": "a" * 64}),
    lambda value: value["files"].update({"central/./source.py": "a" * 64}),
    lambda value: value["files"].update({"central/source.py": "A" * 64}),
    lambda value: value["files"].update({"central/source.py": 123}),
    lambda value: value["helper_bundle"].update(schema=False),
    lambda value: value["helper_bundle"].update(extra="invalid"),
    lambda value: value["helper_bundle"]["files"].update({"../outside.py": "a" * 64}),
])
def test_shared_result_rejects_malformed_or_inconsistent_evidence(source_trees, mutation):
    app, harness, _, _ = source_trees
    value = collect_provenance(app, harness).model_dump(mode="json", by_alias=True)
    mutated = copy.deepcopy(value)
    mutation(mutated)
    with pytest.raises(ValueError):
        decode_provenance(json.dumps(mutated))


@pytest.mark.parametrize("payload", [
    "null", "[]", "true", "1", "{}", "not json", '{"schema":1,"schema":1}',
    " " * (MAX_PROVENANCE_BYTES + 1),
])
def test_shared_result_rejects_invalid_bounded_wire_payload(payload):
    with pytest.raises(ValueError):
        decode_provenance(payload)


def test_duplicate_bundle_keys_are_not_silently_collapsed(source_trees):
    app, harness, _, _ = source_trees
    (harness / "bundle.json").write_text('{"schema":1,"schema":1,"files":{}}')
    process = run_helper(app, harness)
    assert process.returncode == 1
    assert process.stderr == '{"error":"runtime_provenance_invalid"}\n'
