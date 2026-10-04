"""The V2 node deployment: an immutable boot target, and how one is derived from a release.

Pure (kernel and contracts only, no persistence), so it sits below infra: the infra deployment
writer and the fleet services share one model without infra importing fleet. A deployment is what `node_boot_policy` selects and what a boot offer freezes.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Final
from uuid import UUID, uuid4

from central.kernel.assets import OriginLocator
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_boot import (
    MAX_NODE_BOOT_BYTES,
    NodeBaseRefV2,
    NodeBootOfferV2,
    NodeBootRequestV2,
)
from contracts.node_protocol import digest
from contracts.node_release import NodeReleaseV2
from contracts.strict_json import loads_object

# How long a boot offer stays valid (its `expires_at`), and how long a live offer keeps its
# deployment's files desired (the cleaner never removes what a Pi mid-prepare still needs).
OFFER_TTL_SECONDS: Final = 3600


@dataclass(frozen=True, slots=True)
class NodeDeployment:
    deployment_id: UUID
    base: NodeBaseRefV2
    app_environment: AppEnvironmentRefV2 | None
    manager_primary: AppEnvironmentRefV2
    manager_fallback: AppEnvironmentRefV2 | None
    environment_sources: dict[str, str]

    def offer(self, *, offer_id: UUID, audience: str, request: NodeBootRequestV2,
              device_id: str, generation: int, revision: int, now: float) -> NodeBootOfferV2:
        return NodeBootOfferV2(offer_id, audience, request.serial, device_id, generation,
            request.kernel_boot_id, request.boot_nonce, revision, uuid4(), int(now * 1000),
            int((now + OFFER_TTL_SECONDS) * 1000), self.base,
            "selected" if self.app_environment is not None else "unconfigured", self.app_environment,
            self.manager_primary, self.manager_fallback)

    def environments(self) -> tuple[AppEnvironmentRefV2, ...]:
        """The app (when selected) and the managers, each sealed environment once."""
        refs = (self.app_environment, self.manager_primary, self.manager_fallback)
        return tuple({ref.environment_sha256: ref for ref in refs if ref is not None}.values())


def parse_node_deployment(raw: bytes) -> NodeDeployment:
    value = loads_object(raw, max_bytes=MAX_NODE_BOOT_BYTES)
    fields = {"deployment_id", "base", "app_environment", "manager_primary", "manager_fallback", "environment_sources"}
    if value is None or set(value) != fields or type(value["environment_sources"]) is not dict:
        raise ValueError("node_deployment_invalid")
    try:
        value["deployment_id"] = UUID(value["deployment_id"])
        value["base"] = NodeBaseRefV2(**value["base"])
        for name in ("app_environment", "manager_primary", "manager_fallback"):
            if value[name] is not None:
                value[name] = AppEnvironmentRefV2(**value[name])
        deployment = NodeDeployment(**value)
        # Reuse the canonical compatibility and bounds contract, never parallel rules.
        deployment.offer(offer_id=uuid4(), audience="validation", request=NodeBootRequestV2(
            "fixture", uuid4(), "0" * 64), device_id="device-" + "0" * 64,
            generation=1, revision=1, now=1)
        refs = [item for item in (deployment.app_environment, deployment.manager_primary,
                                 deployment.manager_fallback) if item is not None]
        if set(deployment.environment_sources) != {item.environment_sha256 for item in refs}:
            raise ValueError("node_environment_sources_mismatch")
        for item in refs:
            OriginLocator(deployment.environment_sources[item.environment_sha256],
                          item.environment_sha256, item.size_bytes)
        return deployment
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError("node_deployment_invalid") from exc


def encode_node_deployment(value: NodeDeployment) -> bytes:
    document = asdict(value)
    document["deployment_id"] = str(value.deployment_id)
    raw = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    parse_node_deployment(raw)
    return raw


def deployment_id_for(manifest_sha256: str, with_app: bool) -> UUID:
    """The id of the deployment a release becomes: the first 128 bits of its manifest digest
    with the UUID version set to 8 and the RFC 4122 variant, and the variant nibble's next bit
    set for "with app". The console's former publish rule (`releases.js` `deploymentIdFor`), so
    a deployment the console already published converges as a duplicate."""
    digest(manifest_sha256)
    if type(with_app) is not bool:
        raise ValueError("invalid_with_app")
    hexits = list(manifest_sha256[:32])
    hexits[12] = "8"
    hexits[16] = format(0x8 | (0x2 if with_app else 0) | (int(hexits[16], 16) & 0x1), "x")
    return UUID("".join(hexits))


def deployment_for_release(manifest_sha256: str, release: NodeReleaseV2,
                           locators: Mapping[str, Mapping[str, object]]) -> NodeDeployment:
    """The deployment a valid release becomes, its app included when it carries one.

    `locators` are the catalog row's `asset_locators` (role -> {url, sha256, size})."""
    roles = {release.manager_primary.environment_sha256: "manager-primary"}
    if release.manager_fallback is not None:
        roles[release.manager_fallback.environment_sha256] = "manager-fallback"
    if release.app_environment is not None:
        roles[release.app_environment.environment_sha256] = "app"
    return NodeDeployment(
        deployment_id_for(manifest_sha256, release.app_environment is not None), release.base,
        release.app_environment, release.manager_primary, release.manager_fallback,
        {sha: str(locators[role]["url"]) for sha, role in roles.items()})
