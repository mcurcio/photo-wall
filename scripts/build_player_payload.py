#!/usr/bin/env python3
"""Build the inert Player application archive for a base-owned, unprivileged launcher.

The archive carries a computed first-party Python closure only. It has no Debian maintainer
scripts, OS units, Weston configuration or installer authority. Legacy boot trees continue to
use the separately built Player .deb.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import subprocess
import tarfile
import tempfile
from pathlib import Path

from contracts.player_payload import (
    ENTRYPOINT,
    FORMAT,
    SCHEMA,
    PayloadError,
    archive_name,
    base_abi,
    canonical_json,
    validate_manifest,
    verify_archive,
)
from scripts.build_bootstrapper_deb import launcher_contract_digest
from scripts.build_player_deb import BuildError, assert_declaration_matches, fetch_tree
from scripts.debian_packages import PIN, packages
from scripts.module_closure import (
    PLAYER_POLICY,
    ClosureError,
    closure_for,
    stage_application,
    unreached_imports,
)


def build(repository: Path, revision: str, output_dir: Path,
          *, source_date_epoch: int | None = None) -> Path:
    """Archive committed source deterministically, then verify the entire output before return."""
    filename = archive_name(revision)
    epoch = PIN.epoch if source_date_epoch is None else source_date_epoch
    if type(epoch) is not int or epoch < 0:
        raise PayloadError("source_date_epoch_invalid")
    repository = repository.resolve(strict=True)
    output_dir = output_dir.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix=".photo-wall-player-payload-", dir=output_dir) as tmp:
        tree = Path(tmp) / "tree"
        fetch_tree(repository, revision, tree)
        assert_declaration_matches(tree)
        closure = closure_for(PLAYER_POLICY, repo=tree)
        if missing := unreached_imports(closure, PLAYER_POLICY):
            raise BuildError(f"unreached Player imports: {', '.join(missing)}")
        app = Path(tmp) / ENTRYPOINT
        stage_application(closure, PLAYER_POLICY, repo=tree, into=app)
        records: dict[str, dict[str, str | int]] = {}
        staged = sorted(app.rglob("*"))
        for path in staged:
            if path.is_symlink() or (not path.is_dir() and not path.is_file()):
                raise PayloadError("staged_member_invalid")
            if not path.is_file():
                continue
            data = path.read_bytes()
            name = path.relative_to(Path(tmp)).as_posix()
            records[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        manifest = validate_manifest({
            "schema": SCHEMA,
            "format": FORMAT,
            "revision": revision,
            "base_abi": base_abi(PIN.snapshot, packages("player"),
                                 launcher_contract_digest(tree)),
            "entrypoint": ENTRYPOINT,
            "files": records,
        })
        manifest_path = Path(tmp) / "manifest.json"
        manifest_path.write_bytes(canonical_json(manifest))
        output = output_dir / filename
        with (open(output, "xb") as raw,
              gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped,
              tarfile.open(fileobj=zipped, mode="w", format=tarfile.USTAR_FORMAT) as archive):
            for path in (manifest_path, *(Path(tmp) / name for name in sorted(records))):
                info = archive.gettarinfo(str(path), arcname=path.relative_to(tmp).as_posix())
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = epoch
                info.mode = 0o644
                with path.open("rb") as source:
                    archive.addfile(info, source)
        try:
            if verify_archive(output) != manifest:
                raise PayloadError("built_manifest_mismatch")
        except Exception:
            output.unlink(missing_ok=True)
            raise
        return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--source-date-epoch", type=int)
    args = parser.parse_args()
    try:
        output = build(args.repository, args.revision, args.output_dir,
                       source_date_epoch=args.source_date_epoch)
    except (ValueError, OSError, subprocess.SubprocessError, ClosureError) as error:
        parser.exit(1, f"Player payload build failed: {error}\n")
    print(output)


if __name__ == "__main__":
    main()
