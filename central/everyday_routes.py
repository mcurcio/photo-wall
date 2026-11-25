"""The everyday actions' routes (decision 0036, slice 3; a composition-root module like
home_routes.py), the shapes central/everyday/views.py's:

| Method | Path | Answer |
|---|---|---|
| POST | /v1/operator/walls/{surface_id}/skips | SkipAnswer: every Frame of the wall moved at one instant, or why it stayed |
| POST | /v1/operator/frames/{frame_id}/skips | SkipAnswer: that Frame alone |
| POST | /v1/operator/never-show | NeverShowAnswer: where it was skipped, who stayed, its last showings |
| DELETE | /v1/operator/never-show/{asset_id} | {"asset_id", "was_listed"}: Undo and Show again |
| GET | /v1/operator/never-show | NeverShowListView: Scenes › Never show list |
| POST | /v1/operator/pauses | PauseAnswer: each Frame holding its photo until the pause's end, or why it stayed |
| DELETE | /v1/operator/pauses/{pause_id} | CarryOnAnswer: Carry on, every Frame of the pause |
| DELETE | /v1/operator/pauses/{pause_id}/frames/{frame_id} | CarryOnAnswer: Carry on, that Frame alone |

Show now on chosen Frames with an end (`POST activations`) and Back to normal (`POST
runs/{run_id}/end`) are the Runtime's, declared with its routes in central/app.py.

Each change is an everyday action, so it asks for `operate` (central/operator_auth.py) and stamps
the person; the list is a read (`admin`). A body that does not read answers 422
`invalid_operator_skip` / `invalid_never_show` / `invalid_operator_pause`; an unknown wall, Frame
or pause 404; no Frame able to move 409 `nothing-moved` (a pause: `nothing-paused`) with
`stayed`; a Frame whose photo turned over 409 `changed`; a lock that times out 503 `lock_busy`
(central/lock_waits.py).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Final

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ValidationError

from central.actor import Actor
from central.coordination import Coordinator
from central.everyday.model import EverydayRefusal, SkipScope
from central.everyday.ports import FrameFactsReader
from central.everyday.views import (
    CarryOnAnswer,
    NeverShowAnswer,
    NeverShowListView,
    NeverShowRequest,
    PauseAnswer,
    PauseRequest,
    SkipAnswer,
    SkipRequest,
)
from central.operator_auth import OPERATOR_PREFIX
from central.registry import RegistryError
from central.request_body import capped_body
from contracts.identifiers import Identifier

WALL_SKIPS: Final = OPERATOR_PREFIX + "walls/{surface_id}/skips"
FRAME_SKIPS: Final = OPERATOR_PREFIX + "frames/{frame_id}/skips"
NEVER_SHOW: Final = OPERATOR_PREFIX + "never-show"
SHOW_AGAIN: Final = OPERATOR_PREFIX + "never-show/{asset_id}"
PAUSES: Final = OPERATOR_PREFIX + "pauses"
CARRY_ON: Final = OPERATOR_PREFIX + "pauses/{pause_id}"
CARRY_ON_FRAME: Final = OPERATOR_PREFIX + "pauses/{pause_id}/frames/{frame_id}"
MAX_BODY_BYTES: Final = 4096


async def _parsed[T: BaseModel](request: Request, model: type[T], code: str) -> T:
    raw = await capped_body(request, MAX_BODY_BYTES, lambda: RegistryError(code, 413))
    try:
        return model.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as exc:
        raise RegistryError(code, 422) from exc


def mount_everyday_routes(app: FastAPI, *, admin: Any, operate: Any, coordinator: Coordinator,
                          facts: FrameFactsReader) -> None:
    """Declare the routes above on `app`. `facts`: Frame status's judgement of each Frame
    (central/infra/frame_status_store.py `PgFrameStatus.everyday_in`), so the commands and Home's
    `can` read one rule."""

    async def skip(scope: SkipScope, target_id: str, request: Request, by: Actor) -> SkipAnswer:
        asked = await _parsed(request, SkipRequest, EverydayRefusal.INVALID_SKIP.value)
        return await asyncio.to_thread(coordinator.skip, scope, target_id, asked.kind, asked.request_id,
                                       by=by, facts=facts)

    @app.post(WALL_SKIPS, response_model=SkipAnswer)
    async def wall_skip(surface_id: Identifier, request: Request, by: Actor = Depends(operate)) -> SkipAnswer:
        return await skip(SkipScope.WALL, surface_id, request, by)

    @app.post(FRAME_SKIPS, response_model=SkipAnswer)
    async def frame_skip(frame_id: Identifier, request: Request, by: Actor = Depends(operate)) -> SkipAnswer:
        return await skip(SkipScope.FRAME, frame_id, request, by)

    @app.post(NEVER_SHOW, response_model=NeverShowAnswer)
    async def never_show(request: Request, by: Actor = Depends(operate)) -> NeverShowAnswer:
        asked = await _parsed(request, NeverShowRequest, EverydayRefusal.INVALID_NEVER_SHOW.value)
        return await asyncio.to_thread(coordinator.never_show, asked.request_id, asked.asset_id,
                                       asked.frame_id, by=by, facts=facts)

    @app.delete(SHOW_AGAIN)
    async def show_again(asset_id: Identifier, by: Actor = Depends(operate)) -> dict:
        return {"asset_id": asset_id, "was_listed": await asyncio.to_thread(coordinator.show_again, asset_id)}

    @app.post(PAUSES, response_model=PauseAnswer)
    async def pause(request: Request, by: Actor = Depends(operate)) -> PauseAnswer:
        asked = await _parsed(request, PauseRequest, EverydayRefusal.INVALID_PAUSE.value)
        return await asyncio.to_thread(coordinator.pause, asked.request_id, asked.frame_ids, asked.seconds,
                                       by=by, facts=facts)

    @app.delete(CARRY_ON, response_model=CarryOnAnswer)
    async def carry_on(pause_id: Identifier, by: Actor = Depends(operate)) -> CarryOnAnswer:
        return await asyncio.to_thread(coordinator.carry_on, pause_id, None, by=by)

    @app.delete(CARRY_ON_FRAME, response_model=CarryOnAnswer)
    async def carry_on_frame(pause_id: Identifier, frame_id: Identifier,
                             by: Actor = Depends(operate)) -> CarryOnAnswer:
        return await asyncio.to_thread(coordinator.carry_on, pause_id, frame_id, by=by)

    @app.get(NEVER_SHOW, dependencies=[Depends(admin)], response_model=NeverShowListView)
    async def never_show_list() -> NeverShowListView:
        return await asyncio.to_thread(coordinator.never_show_entries)


__all__ = ["CARRY_ON", "CARRY_ON_FRAME", "FRAME_SKIPS", "NEVER_SHOW", "PAUSES", "SHOW_AGAIN", "WALL_SKIPS",
           "mount_everyday_routes"]
