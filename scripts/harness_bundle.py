"""Explicit, importable source bundles for isolated acceptance helpers."""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


def wall_helper_dockerfile(base_image: str, *, include_release: bool = False) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.:/@-]+", base_image) is None:
        raise ValueError("harness_base_image")
    return (
        f"FROM {base_image}\n"
        # Stage a traversable directory before copying files to explicit paths;
        # chmod on a multi-source COPY can otherwise chmod the new directory.
        "COPY scripts /harness/scripts/\n"
        "COPY --chmod=0644 demo_wall.py /harness/demo_wall.py\n"
        "COPY --chmod=0644 bundle.json /harness/bundle.json\n"
        + ("COPY release /release/\n" if include_release else "")
    )


@dataclass(frozen=True)
class BundleFile:
    source: str
    target: str


@dataclass(frozen=True)
class HarnessBundle:
    files: tuple[BundleFile, ...]

    def stage(self, source_root: Path, target_root: Path) -> dict[str, str]:
        """Copy the closed dependency set while preserving its import layout."""
        result = {}
        for item in self.files:
            source = (source_root / item.source).resolve()
            target = (target_root / item.target).resolve()
            if source_root.resolve() not in source.parents or target_root.resolve() not in target.parents:
                raise ValueError("harness_bundle_path")
            if not source.is_file() or source.is_symlink():
                raise ValueError("harness_bundle_source")
            target.parent.mkdir(parents=True, exist_ok=True)
            # Public image helpers must remain readable/traversable under any
            # host umask. Keep the enclosing private staging root unchanged.
            parent = target.parent
            while parent != target_root.resolve():
                parent.chmod(0o755)
                parent = parent.parent
            shutil.copyfile(source, target)
            target.chmod(0o644)
            result[item.target] = hashlib.sha256(target.read_bytes()).hexdigest()
        return result


WALL_HELPER_BUNDLE = HarnessBundle(files=(
    BundleFile("scripts/demo_wall.py", "demo_wall.py"),
    BundleFile("scripts/harness_failure.py", "scripts/harness_failure.py"),
    BundleFile("scripts/immich_actions.py", "scripts/immich_actions.py"),
    BundleFile("scripts/provenance_models.py", "scripts/provenance_models.py"),
    BundleFile("scripts/runtime_provenance.py", "scripts/runtime_provenance.py"),
))

IMMICH_RUNTIME_BUNDLE = HarnessBundle(files=(
    BundleFile("scripts/harness_failure.py", "scripts/harness_failure.py"),
    BundleFile("scripts/immich_actions.py", "scripts/immich_actions.py"),
    BundleFile("scripts/immich_runtime.py", "scripts/immich_runtime.py"),
))
