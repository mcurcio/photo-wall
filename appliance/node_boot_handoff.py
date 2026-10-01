"""Initramfs-safe V2 nonce and exact handoff; no app or effect dependencies."""
from __future__ import annotations

import json
import secrets
from pathlib import Path
from uuid import UUID

from contracts.node_boot import (
    MAX_NODE_BOOT_BYTES,
    NodeBootOfferV2,
    encode_node_boot_offer,
    parse_node_boot_offer,
)
from contracts.strict_json import loads_object
from uplink.files import write_atomically

HANDOFF = Path("etc/photo-wall/node-boot.json")


def node_nonce(run_root: Path, boot_id: UUID) -> str:
    path = run_root / "node-boot-nonce.json"
    if path.exists() or path.is_symlink():
        if path.is_symlink():
            raise ValueError("node_nonce_symlink")
        value = loads_object(path.read_bytes(), max_bytes=256)
        if value is None or set(value) != {"boot_id", "nonce"} or value["boot_id"] != str(boot_id) or not isinstance(value["nonce"], str) or len(value["nonce"]) != 64:
            raise ValueError("node_nonce_invalid")
        return value["nonce"]
    nonce = secrets.token_hex(32)
    write_atomically(path, json.dumps({"boot_id": str(boot_id), "nonce": nonce}).encode(), mode=0o600)
    return nonce


def write_node_handoff(root: Path, *, central: str, offer: NodeBootOfferV2) -> Path:
    value = {"schema": 2, "central": central, "offer": json.loads(encode_node_boot_offer(offer))}
    path = root / HANDOFF
    write_atomically(path, json.dumps(value, sort_keys=True).encode(), mode=0o600)
    return path


def read_node_handoff(path: Path) -> tuple[str, NodeBootOfferV2]:
    info = path.lstat()
    if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o077 or info.st_size > MAX_NODE_BOOT_BYTES + 2048:
        raise ValueError("node_handoff_ownership_or_bound")
    value = loads_object(path.read_bytes(), max_bytes=MAX_NODE_BOOT_BYTES + 2048)
    if value is None or set(value) != {"schema", "central", "offer"} or value["schema"] != 2 or not isinstance(value["central"], str):
        raise ValueError("node_handoff_invalid")
    return value["central"], parse_node_boot_offer(json.dumps(value["offer"]).encode())
