"""Home's routes (decision 0036; a composition-root module like central_errors_routes.py), the
shapes central/frame_status/views.py's:

| Method | Path | Answer |
|---|---|---|
| GET | /v1/operator/home | HomeView: the house word, each Wall's Frames with their status, the overrides, now and next |
| GET | /v1/operator/frames/{frame_id}/status | FrameStatusView: the Frame's status and its recent changes |

Both take the operator `admin` dependency (a read). Home answers with `ETag: "<change>"`; asked
with `If-None-Match` naming the document's current change, it answers 304 with no body, so an
unchanged wall costs none. A store failure answers 500: never "all good".

`ACTION_PATHS` names where the console asks each action a Frame's `can` offers (the routes'
owners: central/fleet/node_routes.py, central/fleet/app_restart.py, central/everyday_routes.py).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Protocol

from fastapi import Depends, FastAPI, Request, Response

from central.everyday_routes import FRAME_SKIPS, NEVER_SHOW
from central.fleet.app_restart import APP_RESTARTS_PATH
from central.frame_status.model import Action
from central.frame_status.views import FrameStatusView, HomeView
from central.operator_auth import OPERATOR_PREFIX
from central.registry import RegistryError
from contracts.identifiers import Identifier

HOME: Final = OPERATOR_PREFIX + "home"
FRAME_STATUS: Final = OPERATOR_PREFIX + "frames/{frame_id}/status"
# Where each action is asked, `{device_id}` the Frame's Pi.
REBOOTS: Final = OPERATOR_PREFIX + "node/devices/{device_id}/reboots"
APP_RESTARTS: Final = APP_RESTARTS_PATH        # central/fleet/app_restart.py's, the address's one home
# Skip and Previous are one route (the body's `kind`); a Wall's are WALL_SKIPS
# (central/everyday_routes.py).
ACTION_PATHS: Final[Mapping[Action, str]] = {
    Action.REBOOT_PI: REBOOTS, Action.RESTART_APP: APP_RESTARTS, Action.SKIP: FRAME_SKIPS,
    Action.PREVIOUS: FRAME_SKIPS, Action.NEVER_SHOW: NEVER_SHOW}


class HomeQueries(Protocol):
    """Home's reads (central/infra/frame_status_store.py `PgFrameStatus`)."""

    def home(self) -> HomeView: ...

    def frame(self, frame_id: str) -> FrameStatusView:
        """LookupError for no such Frame."""
        ...


def etag(change: int) -> str:
    """The entity tag of a document at `change` (a strong tag: equal documents, equal numbers)."""
    return f'"{change}"'


def _matches(header: str | None, tag: str) -> bool:
    """Whether an `If-None-Match` header names `tag` (weak comparison, RFC 9110 §13.1.2)."""
    if header is None:
        return False
    named = {each.strip().removeprefix("W/") for each in header.split(",")}
    return "*" in named or tag in named


def mount_home_routes(app: FastAPI, *, admin: Any, home: HomeQueries) -> None:
    """Declare the routes above on `app`."""

    @app.get(HOME, dependencies=[Depends(admin)], response_model=HomeView,
             responses={304: {"description": "The document is at the change If-None-Match names."}})
    def read_home(request: Request, response: Response) -> Any:
        view = home.home()
        tag = etag(view.change)
        if _matches(request.headers.get("if-none-match"), tag):
            return Response(status_code=304, headers={"ETag": tag})
        response.headers["ETag"] = tag
        return view

    @app.get(FRAME_STATUS, dependencies=[Depends(admin)], response_model=FrameStatusView)
    def frame_status(frame_id: Identifier) -> FrameStatusView:
        try:
            return home.frame(frame_id)
        except LookupError as exc:
            raise RegistryError("unknown_frame", 404) from exc


__all__ = ["ACTION_PATHS", "APP_RESTARTS", "FRAME_STATUS", "HOME", "REBOOTS", "HomeQueries", "etag", "mount_home_routes"]
