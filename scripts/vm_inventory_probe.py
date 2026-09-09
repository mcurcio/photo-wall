"""Validate the operator inventory contract and emit its enrollment projection."""

from __future__ import annotations

import os
import ssl
import urllib.request

from pydantic import TypeAdapter

from central.installation_models import EquipmentSessionObservation, InstallationInventory

MAX_INVENTORY_BYTES = 1024**2
SESSIONS = TypeAdapter(tuple[EquipmentSessionObservation, ...])


def enrollment_sessions(payload: bytes) -> tuple[EquipmentSessionObservation, ...]:
    """Validate the Installation response before reducing it to session facts.

    Enrollment is observable before optional health/renderer feedback arrives.
    Neither those delayed fields nor private health content enter this projection.
    """
    if len(payload) > MAX_INVENTORY_BYTES:
        raise ValueError("inventory_limit")
    inventory = InstallationInventory.model_validate_json(payload)
    return tuple(EquipmentSessionObservation(
        player_id=player.id, device_id=player.device_id, authority_epoch=player.authority_epoch,
        retired=player.retired_at is not None,
    ) for player in inventory.players)


def main() -> None:
    context = ssl.create_default_context(cafile="/public/ca.pem")
    request = urllib.request.Request(
        "https://photo-wall.test/v1/operator/inventory",
        headers={"Authorization": "Bearer " + os.environ["PHOTO_WALL_ADMIN_TOKEN"]},
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
    )
    with opener.open(request, timeout=5) as response:
        data = response.read(MAX_INVENTORY_BYTES + 1)
    print(SESSIONS.dump_json(enrollment_sessions(data)).decode())


if __name__ == "__main__":
    main()
