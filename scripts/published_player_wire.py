#!/usr/bin/env python3
"""Pin and exercise the exact v0.12/v0.13 published Player package wire models.

`prepare` is opt-in locally and mandatory in CI. The child commands run in a
fresh isolated Python interpreter with the extracted package first on sys.path.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace


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


def _load_package(root: Path) -> Path:
    """Require exact-package imports even when an editable checkout is installed."""
    root = root.resolve(strict=True)
    sys.path.insert(0, str(root))
    import contracts.models  # noqa: PLC0415
    import player.service  # noqa: PLC0415
    source = Path(player.service.__file__).resolve(strict=True)
    contract_source = Path(contracts.models.__file__).resolve(strict=True)
    if source != root / "player/service.py" or contract_source != root / "contracts/models.py":
        raise ValueError("published_player_import_escaped_package")
    _require_package_imports(root)
    return source


def _require_package_imports(root: Path) -> None:
    """Do not let a missing packaged dependency resolve to the checkout `.pth`."""
    root = root.resolve(strict=True)
    for name, module in tuple(sys.modules.items()):
        if name in ("player", "contracts", "uplink") or name.startswith(
            ("player.", "contracts.", "uplink.")
        ):
            source = getattr(module, "__file__", None)
            if source is None or not Path(source).resolve(strict=True).is_relative_to(root):
                raise ValueError(f"published_player_import_escaped_package:{name}")


def _child(root: Path, mode: str) -> None:
    """Apply the released REST/WS parser or readiness contract to one wire body."""
    source = _load_package(root)
    from contracts.models import Readiness  # noqa: PLC0415
    from player.service import State, _json  # noqa: PLC0415
    _require_package_imports(root)

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


def _handshake(root: Path, rounds: int) -> None:
    """Run the published PlayerService.enroll method over a parent-relayed HTTP port.

    The parent sends real Central HTTP responses on stdin. The Player's output
    messages name the method, path and exact package-built JSON body. Keep one
    process/ephemeral key across two rounds to exercise cold re-enrollment.
    """
    source = _load_package(root)
    from contracts.enrollment import OutputReport  # noqa: PLC0415
    from player.central_link import Session  # noqa: PLC0415
    from player.identity import load_identity  # noqa: PLC0415
    from player.service import BootContext, PlayerService  # noqa: PLC0415
    _require_package_imports(root)

    identity = load_identity()
    device_id = "device-" + hashlib.sha256(bytes.fromhex(identity.public_key)).hexdigest()
    outputs = (OutputReport(output_id="HDMI-A-1", width_px=1920, height_px=1080),
               OutputReport(output_id="HDMI-A-2", width_px=1920, height_px=1080))

    async def request(method, path, *, body, authenticated):
        print(json.dumps({"event": "request", "source": str(source), "method": method,
                          "path": path, "body": body, "authenticated": authenticated}),
              flush=True)
        line = sys.stdin.readline()
        if not line:
            raise ValueError("published_player_http_response_missing")
        response = json.loads(line)
        if response["status"] != 200:
            raise ValueError(f"published_player_http_refused:{path}:{response['status']}")
        return response["body"]

    probe = SimpleNamespace(
        identity=identity, outputs=outputs, request=request,
        _session=Session(object()), _lock=threading.RLock(), _outgoing=deque(),
        executor=object(), _offered=False, _jobs=(),
    )
    for _ in range(rounds):
        probe.boot_context = BootContext.model_validate({
            "schema": 2, "ticket_id": None, "device_id": device_id,
            "boot_id": str(uuid.uuid4()), "release_id": "a" * 64,
            "trial": False, "persistence": "volatile", "fault": None,
        })
        probe.session = probe._session
        asyncio.run(PlayerService.enroll(probe))
        registered = probe._session.registration
        print(json.dumps({"event": "registered", "source": str(source),
                          "player_id": registered.player_id,
                          "authority_epoch": registered.authority_epoch,
                          "has_token": bool(registered.token)}), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    action = parser.add_subparsers(dest="action", required=True)
    action.add_parser("prepare").add_argument("directory", type=Path)
    child = action.add_parser("child")
    child.add_argument("root", type=Path)
    child.add_argument("mode", choices=("rest", "websocket", "readiness"))
    handshake = action.add_parser("handshake")
    handshake.add_argument("root", type=Path)
    handshake.add_argument("rounds", type=int, choices=(1, 2))
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.directory)
    elif args.action == "child":
        _child(args.root, args.mode)
    else:
        _handshake(args.root, args.rounds)


if __name__ == "__main__":
    main()
