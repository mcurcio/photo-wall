"""Frozen V2 node boot selection; legacy boot offers are separate contracts."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from uuid import UUID

from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_protocol import counter, digest, identifier, token
from contracts.strict_json import loads_object

MAX_NODE_BOOT_BYTES = 16384
MAX_ENVIRONMENT_BYTES = 8 * 1024**3


@dataclass(frozen=True, slots=True)
class NodeBootRequestV2:
    serial: str
    kernel_boot_id: UUID
    boot_nonce: str

    def __post_init__(self) -> None:
        token(self.serial, 128)
        identifier(self.kernel_boot_id)
        digest(self.boot_nonce)


@dataclass(frozen=True, slots=True)
class NodeBaseRefV2:
    tag: str
    content_key: str
    squashfs_sha256: str
    size_bytes: int
    base_abi: str
    graphics_abi: str
    plugin_abi: str

    def __post_init__(self) -> None:
        token(self.tag, 128)
        digest(self.content_key)
        digest(self.squashfs_sha256)
        counter(self.size_bytes, 1)
        if self.size_bytes > MAX_ENVIRONMENT_BYTES:
            raise ValueError("node_base_too_large")
        for value in (self.base_abi, self.graphics_abi, self.plugin_abi):
            token(value)


@dataclass(frozen=True, slots=True)
class NodeBootOfferV2:
    offer_id: UUID
    installation_audience: str
    serial: str
    device_id: str
    device_generation: int
    kernel_boot_id: UUID
    boot_nonce: str
    policy_revision: int
    cold_operation_id: UUID
    created_at_utc_ms: int
    expires_at_utc_ms: int
    base: NodeBaseRefV2
    app_status: str
    app_environment: AppEnvironmentRefV2 | None
    manager_primary: AppEnvironmentRefV2
    manager_fallback: AppEnvironmentRefV2 | None

    def __post_init__(self) -> None:
        for value in (self.offer_id, self.kernel_boot_id, self.cold_operation_id):
            identifier(value)
        token(self.installation_audience, 256)
        token(self.serial, 128)
        if not isinstance(self.device_id, str) or not re.fullmatch(r"device-[0-9a-f]{64}", self.device_id):
            raise ValueError("node_boot_device_invalid")
        digest(self.boot_nonce)
        for value in (self.device_generation, self.policy_revision):
            counter(value, 1)
        counter(self.created_at_utc_ms)
        counter(self.expires_at_utc_ms, 1)
        if self.expires_at_utc_ms <= self.created_at_utc_ms:
            raise ValueError("node_boot_expiry_invalid")
        if type(self.base) is not NodeBaseRefV2 or type(self.manager_primary) is not AppEnvironmentRefV2:
            raise ValueError("node_boot_base_or_manager_invalid")
        if self.app_status not in ("unconfigured", "selected") or (
                self.app_status == "selected") != (self.app_environment is not None):
            raise ValueError("node_boot_app_selection_invalid")
        validate_node_environment_roles(self.base, self.app_environment, self.manager_primary, self.manager_fallback)


# Each release root's package (decision 0019, debian/control), which its reference's deb_name
# names: the manager root's AppManager launcher and the app root's Player. Judged where a root
# is built (scripts/seal_root.py, scripts/node_release_writer.py) and launched
# (appliance/node/manager_launcher.py, appliance/apps/process_linux.py), never when a stored
# document is parsed: Central re-reads releases, deployments and offers an earlier build wrote,
# whose roots named the packages of their day (E-0019-FIX-16).
MANAGER_PACKAGE = "photo-wall-app-manager"
APP_PACKAGE = "photo-wall-player"


def validate_node_environment_roles(base, app_environment, manager_primary, manager_fallback):
    """Shared cold-offer/release role and ABI compatibility rules."""
    if manager_fallback is not None and manager_fallback.environment_sha256 == manager_primary.environment_sha256:
        raise ValueError("node_boot_role_invalid")
    for environment in (app_environment, manager_primary, manager_fallback):
        if environment is None:
            continue
        if type(environment) is not AppEnvironmentRefV2 or environment.size_bytes > MAX_ENVIRONMENT_BYTES:
            raise ValueError("node_boot_environment_invalid")
        if (environment.base_abi, environment.graphics_abi, environment.plugin_abi) != (
                base.base_abi, base.graphics_abi, base.plugin_abi):
            raise ValueError("node_boot_environment_abi_mismatch")


def _json(value: dict) -> bytes:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_NODE_BOOT_BYTES:
        raise ValueError("node_boot_too_large")
    return raw


def encode_node_boot_request(value: NodeBootRequestV2) -> bytes:
    return _json({"schema": 2, "kind": "node_boot_request", "serial": value.serial,
                  "kernel_boot_id": str(value.kernel_boot_id), "boot_nonce": value.boot_nonce})


def parse_node_boot_request(raw: bytes) -> NodeBootRequestV2:
    value = loads_object(raw, max_bytes=MAX_NODE_BOOT_BYTES)
    if (value is None or set(value) != {"schema", "kind", "serial", "kernel_boot_id", "boot_nonce"}
            or type(value["schema"]) is not int or value["schema"] != 2
            or value["kind"] != "node_boot_request"):
        raise ValueError("node_boot_request_invalid")
    try:
        return NodeBootRequestV2(value["serial"], UUID(value["kernel_boot_id"]), value["boot_nonce"])
    except (TypeError, AttributeError) as exc:
        raise ValueError("node_boot_request_invalid") from exc


def encode_node_boot_offer(value: NodeBootOfferV2) -> bytes:
    result = asdict(value)
    for name in ("offer_id", "kernel_boot_id", "cold_operation_id"):
        result[name] = str(result[name])
    return _json({"schema": 2, "kind": "node_boot_offer", **result})


def parse_node_boot_offer(raw: bytes) -> NodeBootOfferV2:
    value = loads_object(raw, max_bytes=MAX_NODE_BOOT_BYTES)
    if (value is None or type(value.get("schema")) is not int or value.pop("schema") != 2
            or value.pop("kind", None) != "node_boot_offer"):
        raise ValueError("node_boot_offer_invalid")
    try:
        for name in ("offer_id", "kernel_boot_id", "cold_operation_id"):
            value[name] = UUID(value[name])
        value["base"] = NodeBaseRefV2(**value["base"])
        for name in ("app_environment", "manager_primary", "manager_fallback"):
            if value[name] is not None:
                value[name] = AppEnvironmentRefV2(**value[name])
        return NodeBootOfferV2(**value)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("node_boot_offer_invalid") from exc
