"""Adding Galleria: one entry per Central, by its origin (the address the console is reached at)."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import CONF_ORIGIN, DOMAIN
from .contract import CENTRAL_BIRTH_KEY, CentralBirth, decode_central_state
from .door import CentralDoor, DoorUnavailable, normalized_origin

_SCHEMA = vol.Schema({vol.Required(CONF_ORIGIN): str})


async def central_birth(origin: str) -> CentralBirth | None:
    """Central's birth read through `origin`'s door (None: the door answers but holds no Central
    birth this Galleria reads); DoorUnavailable when the door does not answer."""
    door = await CentralDoor.connect(origin)
    try:
        raw = await door.read(CENTRAL_BIRTH_KEY)
    finally:
        await door.close()
    try:
        return decode_central_state(CENTRAL_BIRTH_KEY, raw) if raw is not None else None
    except ValueError:
        return None


class GalleriaConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """{origin}; validated by reading Central's birth through its door; one entry per Central
        (unique id: the normalized origin)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                origin = normalized_origin(user_input[CONF_ORIGIN])
            except ValueError:
                errors[CONF_ORIGIN] = "invalid_origin"
            else:
                await self.async_set_unique_id(origin)
                self._abort_if_unique_id_configured()
                try:
                    birth = await central_birth(origin)
                except DoorUnavailable:
                    errors["base"] = "cannot_connect"
                else:
                    if birth is None:
                        errors["base"] = "not_central"
                    else:
                        return self.async_create_entry(title=f"Galleria ({urlsplit(origin).netloc})",
                                                       data={CONF_ORIGIN: origin})
        return self.async_show_form(step_id="user", data_schema=self.add_suggested_values_to_schema(
            _SCHEMA, user_input or {}), errors=errors)
