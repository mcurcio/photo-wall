#!/usr/bin/env python3
"""Pin and exercise the exact v0.12/v0.13 published Player package wire models.

`prepare` is opt-in locally and mandatory in CI. `parse` and `readiness` run in a
fresh isolated Python interpreter with the extracted package first on sys.path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PublishedPlayer:
    tag: str
    filename: str
    sha256: str
    size: int


PLAYERS = (
    PublishedPlayer(
        "v0.12.0",
        "photo-wall-player_0.1.0+g27dbe4ac7f5a939b2e15b6ea2adb50a1ddc27a13_all.deb",
        "3206c9f5f48655f2fba4fed81e6565f690f5ecccac1aec25017f3383c4ee67ad",
        71918,
    ),
    PublishedPlayer(
        "v0.13.0",
        "photo-wall-player_0.1.0+g08c767b80386c749ac27bc943132767f0d5a1608_all.deb",
        "b1346644fa65cf81b8bae88fbc951fa76ca6677a5b4b91bc9d7067f6c73f71dd",
        73768,
    ),
)


def package_root(directory: Path, player: PublishedPlayer) -> Path:
    return directory / player.tag / "root/usr/lib/photo-wall-player"


def _verified_package(path: Path, player: PublishedPlayer) -> bool:
    if not path.is_file() or path.stat().st_size != player.size:
        return False
    return hashlib.sha256(path.read_bytes()).hexdigest() == player.sha256


def _download(path: Path, player: PublishedPlayer) -> None:
    url = (f"https://github.com/mcurcio/photo-wall/releases/download/{player.tag}/"
           + urllib.parse.quote(player.filename))
    request = urllib.request.Request(url, headers={"User-Agent": "photo-wall-f0-wire-gate"})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read(player.size + 1)
    if len(body) != player.size or hashlib.sha256(body).hexdigest() != player.sha256:
        raise ValueError(f"published_player_digest_mismatch:{player.tag}")
    path.write_bytes(body)


def prepare(directory: Path) -> None:
    """Fetch digest-pinned release assets and extract data without installing them."""
    directory.mkdir(parents=True, exist_ok=True)
    for player in PLAYERS:
        release_dir = directory / player.tag
        release_dir.mkdir(exist_ok=True)
        package = release_dir / player.filename
        if not _verified_package(package, player):
            _download(package, player)
        root = release_dir / "root"
        with tempfile.TemporaryDirectory(prefix=".extract-", dir=release_dir) as temporary:
            extracted = Path(temporary)
            if shutil.which("dpkg-deb"):
                subprocess.run(["dpkg-deb", "-x", str(package), str(extracted)], check=True)
            else:
                archive = subprocess.run(["ar", "p", str(package), "data.tar.zst"],
                                         check=True, capture_output=True).stdout
                subprocess.run(["tar", "-xf", "-", "-C", str(extracted)],
                               input=archive, check=True)
            if not (extracted / "usr/lib/photo-wall-player/player/service.py").is_file():
                raise ValueError(f"published_player_code_missing:{player.tag}")
            if root.exists():
                shutil.rmtree(root)
            extracted.rename(root)


def _child(root: Path, mode: str) -> None:
    """Load the extracted package, never an editable checkout, for one wire exchange."""
    root = root.resolve(strict=True)
    sys.path.insert(0, str(root))
    import contracts.models  # noqa: PLC0415
    import player.service  # noqa: PLC0415
    from contracts.models import Readiness  # noqa: PLC0415
    from player.service import State, _json  # noqa: PLC0415

    source = Path(player.service.__file__).resolve(strict=True)
    contract_source = Path(contracts.models.__file__).resolve(strict=True)
    if source != root / "player/service.py" or contract_source != root / "contracts/models.py":
        raise ValueError("published_player_import_escaped_package")
    body = _json(sys.stdin.buffer.read())
    if mode == "readiness":
        report = Readiness.model_validate(body)
        print(json.dumps({"source": str(source), "body": report.model_dump(mode="json")}))
        return
    if mode == "websocket" and body.pop("type", None) != "state":
        raise ValueError("published_player_message_type")
    from pydantic import ValidationError  # noqa: PLC0415
    try:
        state = State.model_validate(body)
    except ValidationError as error:
        print(json.dumps({"source": str(source), "accepted": False,
                          "errors": [item["type"] for item in error.errors()]}))
    else:
        print(json.dumps({"source": str(source), "accepted": True,
                          "has_plan": state.plan is not None,
                          "commits": len(state.commits)}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    action = parser.add_subparsers(dest="action", required=True)
    action.add_parser("prepare").add_argument("directory", type=Path)
    child = action.add_parser("child")
    child.add_argument("root", type=Path)
    child.add_argument("mode", choices=("rest", "websocket", "readiness"))
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.directory)
    else:
        _child(args.root, args.mode)


if __name__ == "__main__":
    main()
