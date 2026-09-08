"""Collect exact source evidence without importing application or host orchestration."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

# Direct file execution works from /app without relying on a container PYTHONPATH.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.provenance_models import (  # noqa: E402
    CORE_PACKAGES,
    MAX_PROVENANCE_BYTES,
    HelperBundleManifest,
    RuntimeProvenance,
    bounded_json,
    decode_provenance,
    inventory_sha256,
)


def source_digest(root: Path, path: Path) -> str:
    if (path.is_symlink() or not path.is_file()
            or path.resolve() != root.resolve() / path.relative_to(root)):
        raise ValueError("provenance_source")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_inventory(root: Path) -> dict[str, str]:
    paths = sorted(
        path for name in CORE_PACKAGES for path in (root / name).rglob("*")
        if path.suffix in (".py", ".sql")
    )
    return {
        path.relative_to(root).as_posix(): source_digest(root, path)
        for path in paths
    }


def collect_provenance(app_root: Path, harness_root: Path) -> RuntimeProvenance:
    source_digest(harness_root, harness_root / "bundle.json")
    with (harness_root / "bundle.json").open("rb") as stream:
        manifest = HelperBundleManifest.model_validate(
            bounded_json(stream.read(MAX_PROVENANCE_BYTES + 1))
        )
    files = source_inventory(app_root)
    observed_bundle = {
        name: source_digest(harness_root, harness_root / name) for name in manifest.files
    }
    # An undeclared helper source must not silently participate in execution.
    actual_sources = {
        path.relative_to(harness_root).as_posix()
        for path in harness_root.rglob("*") if path.suffix in (".py", ".sql")
    }
    declared_sources = {
        name for name in manifest.files if Path(name).suffix in (".py", ".sql")
    }
    if observed_bundle != manifest.files or actual_sources != declared_sources:
        raise ValueError("provenance_bundle_mismatch")
    result = RuntimeProvenance.model_validate({
        "schema": 1,
        "files": files,
        "core_inventory_sha256": inventory_sha256(files),
        "adapter_sha256": files["media/immich.py"],
        "preparer_sha256": files["media/prepare.py"],
        "harness_sha256": source_digest(harness_root, harness_root / "demo_wall.py"),
        "helper_bundle": {"schema": 1, "files": observed_bundle},
    })
    # Apply the same bounded wire decoding used by the host before publishing.
    return decode_provenance(result.model_dump_json(by_alias=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path, default=Path("/app"))
    parser.add_argument("--harness-root", type=Path, default=Path("/harness"))
    args = parser.parse_args(argv)
    try:
        result = collect_provenance(args.app_root, args.harness_root)
    except Exception:
        # No exception text, filesystem contents, or environment enters diagnostics.
        print('{"error":"runtime_provenance_invalid"}', file=sys.stderr)
        return 1
    print(result.model_dump_json(by_alias=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
