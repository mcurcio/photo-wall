"""Galleria's release asset (contracts/release.py `GALLERIA`): the Home Assistant integration as
HACS's `zip_release` installs it, and the check the release's verify reads it back with.

HACS extracts the zip into `custom_components/<domain>/`, so the archive holds the CONTENTS of
`custom_components/galleria/` at its root (`manifest.json`, `__init__.py`, `translations/`, ...),
never the directory itself. `manifest.json`'s "version" is the release tag without its `v`
(v0.35.0 -> 0.35.0): the tag is created by the seal at the built commit, so no commit can carry
its own release's version, and this asset is where the version Home Assistant shows comes from.

Deterministic for one tree and tag: sorted members, one fixed time, fixed modes, no
`__pycache__`. Stdlib only; runs on the runner's python3 inside the seal
(scripts/package_release_artifacts.py), or by hand:

    python3 -m scripts.galleria_zip --tag v0.35.0 --out <dir>/galleria.zip
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
import zlib
from pathlib import Path
from typing import Final

DOMAIN: Final = "galleria"
# The checkout's integration: in the seal, the built commit's (pipeline.yml checks it out).
SOURCE: Final = Path(__file__).resolve().parents[1] / "custom_components" / DOMAIN
MANIFEST: Final = "manifest.json"
MAX_ZIP_BYTES: Final = 16 * 1024**2
_TIME: Final = (2020, 1, 1, 0, 0, 0)


class GalleriaZipError(ValueError):
    """The integration cannot be zipped for this tag, or a zip is not Galleria's for it."""


def version(tag: str) -> str:
    """The integration's version for the release `tag`: the tag without its `v`. The plan
    (scripts/release_plan.py) already holds the tag to its strict form."""
    if not isinstance(tag, str) or not tag.startswith("v") or len(tag) < 2:
        raise GalleriaZipError(f"galleria_tag_invalid:{tag}")
    return tag.removeprefix("v")


def build(source: Path, tag: str, out: Path) -> Path:
    """Write the new zip `out` from the integration directory `source`, versioned for `tag`."""
    stamped = version(tag)
    files = sorted((path for path in source.rglob("*")
                    if path.is_file() and "__pycache__" not in path.relative_to(source).parts),
                   key=lambda path: path.relative_to(source).as_posix())
    if not (source / MANIFEST).is_file():
        raise GalleriaZipError("galleria_manifest_missing")
    with open(out, "xb") as raw, zipfile.ZipFile(raw, "w") as archive:
        for path in files:
            name = path.relative_to(source).as_posix()
            data = path.read_bytes()
            if name == MANIFEST:
                manifest = json.loads(data)
                if manifest.get("domain") != DOMAIN:
                    raise GalleriaZipError("galleria_manifest_domain_invalid")
                manifest["version"] = stamped
                data = (json.dumps(manifest, indent=2) + "\n").encode()
            info = zipfile.ZipInfo(name, date_time=_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    return out


def check(path: Path, tag: str) -> None:
    """`path` is Galleria's zip for `tag`: a zip whose members stay within it, named once each,
    with `manifest.json` at its root naming the integration and `tag`'s version."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(set(names)) != len(names) or any(
                    name.startswith("/") or "\\" in name or ".." in name.split("/")
                    for name in names):
                raise GalleriaZipError("galleria_zip_member_invalid")
            if MANIFEST not in names:
                raise GalleriaZipError(f"galleria_zip_missing:{MANIFEST}")
            manifest = json.loads(archive.read(MANIFEST))
    except (zipfile.BadZipFile, zlib.error, OSError, json.JSONDecodeError, UnicodeError):
        raise GalleriaZipError("galleria_zip_invalid") from None
    if not isinstance(manifest, dict) or manifest.get("domain") != DOMAIN:
        raise GalleriaZipError("galleria_zip_domain_invalid")
    if manifest.get("version") != version(tag):
        raise GalleriaZipError(f"galleria_zip_version_mismatch:{manifest.get('version')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--out", type=Path, required=True)
    arguments = parser.parse_args(argv)
    print(build(arguments.source, arguments.tag, arguments.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
