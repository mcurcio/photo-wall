"""The Home Assistant settings page's route (a composition-root module like power_routes.py):

| Method | Path | Answer |
|---|---|---|
| GET | /v1/operator/integrations/home-assistant | HomeAssistantStatesView |

The route takes the operator `admin` dependency.
"""
from __future__ import annotations

from typing import Any, Final

from fastapi import Depends, FastAPI

from central.infra.home_assistant.settings import PgHomeAssistantSettings
from central.infra.home_assistant.views import HomeAssistantStatesView
from central.operator_auth import OPERATOR_PREFIX

HOME_ASSISTANT: Final = OPERATOR_PREFIX + "integrations/home-assistant"


def mount_home_assistant_routes(app: FastAPI, *, admin: Any, settings: PgHomeAssistantSettings) -> None:
    """Declare the route above on `app`."""

    @app.get(HOME_ASSISTANT, dependencies=[Depends(admin)], response_model=HomeAssistantStatesView)
    def home_assistant() -> HomeAssistantStatesView:
        return settings.view()


__all__ = ["HOME_ASSISTANT", "mount_home_assistant_routes"]
