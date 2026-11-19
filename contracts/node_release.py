"""Canonical immutable V2 release publication, independent of legacy manifests."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_boot import NodeBaseRefV2, validate_node_environment_roles
from contracts.node_protocol import counter, digest
from contracts.strict_json import loads_object

NODE_RELEASE_MANIFEST = "manifest.node-v2.json"
MAX_NODE_RELEASE_BYTES = 32768
_REQUIRED = frozenset({"base", "boot", "node-base-deb", "node-display-deb", "build-provenance"})
_OPTIONAL = frozenset({"app", "app-deb"})


@dataclass(frozen=True, slots=True)
class NodeReleaseAssetV2:
    role: str
    filename: str
    sha256: str
    size_bytes: int

    def __post_init__(self):
        if self.role not in _REQUIRED | _OPTIONAL:
            raise ValueError("node_release_asset_role_invalid")
        if type(self.filename) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", self.filename):
            raise ValueError("node_release_filename_invalid")
        digest(self.sha256)
        counter(self.size_bytes, 1)
        if self.size_bytes > 8 * 1024**3:
            raise ValueError("node_release_asset_too_large")


@dataclass(frozen=True, slots=True)
class NodeReleaseV2:
    revision: str
    base: NodeBaseRefV2
    app_environment: AppEnvironmentRefV2 | None
    artifacts: tuple[NodeReleaseAssetV2, ...]

    def __post_init__(self):
        if type(self.revision) is not str or not re.fullmatch(r"[0-9a-f]{40}", self.revision):
            raise ValueError("node_release_revision_invalid")
        if type(self.base) is not NodeBaseRefV2:
            raise ValueError("node_release_reference_invalid")
        validate_node_environment_roles(self.base, self.app_environment)
        if (type(self.artifacts) is not tuple or not len(_REQUIRED) <= len(self.artifacts) <= len(_REQUIRED | _OPTIONAL)
                or any(type(x) is not NodeReleaseAssetV2 for x in self.artifacts)):
            raise ValueError("node_release_artifacts_invalid")
        assets = {x.role: x for x in self.artifacts}
        if (len(assets) != len(self.artifacts) or len({x.filename for x in self.artifacts}) != len(self.artifacts)
                or not _REQUIRED <= set(assets)):
            raise ValueError("node_release_artifacts_invalid")
        if assets["base"].sha256 != self.base.content_key:
            raise ValueError("node_release_base_digest_mismatch")
        app = self.app_environment
        if app is None:
            if "app" in assets or "app-deb" in assets:
                raise ValueError("node_release_role_unselected")
        elif (app.architecture != "arm64" or "app" not in assets or "app-deb" not in assets
                or (assets["app"].sha256, assets["app"].size_bytes) != (app.environment_sha256, app.size_bytes)
                or assets["app-deb"].sha256 != app.deb_sha256):
            raise ValueError("node_release_environment_asset_mismatch")


def encode_node_release(value: NodeReleaseV2) -> bytes:
    raw = json.dumps({"schema": 2, "kind": "node_release", **asdict(value)}, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_NODE_RELEASE_BYTES:
        raise ValueError("node_release_too_large")
    return raw


def parse_node_release(raw: bytes) -> NodeReleaseV2:
    value = loads_object(raw, max_bytes=MAX_NODE_RELEASE_BYTES)
    fields = {"schema", "kind", "revision", "base", "app_environment", "artifacts"}
    if (value is None or set(value) != fields or type(value["schema"]) is not int
            or value["schema"] != 2 or value["kind"] != "node_release" or type(value["artifacts"]) is not list):
        raise ValueError("node_release_invalid")
    try:
        return NodeReleaseV2(value["revision"], NodeBaseRefV2(**value["base"]),
            AppEnvironmentRefV2(**value["app_environment"]) if value["app_environment"] is not None else None,
            tuple(NodeReleaseAssetV2(**x) for x in value["artifacts"]))
    except (TypeError, AttributeError) as exc:
        raise ValueError("node_release_invalid") from exc
