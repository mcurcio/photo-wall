"""The operator's Display and power routes (roadmap 1b; run ledger .claude/runs/display-1b.md,
slices C1-C3; a composition-root module like library_routes.py). One route per question:

| Method | Path | Body | Answer | Refusals |
|---|---|---|---|---|
| GET | /v1/operator/frames/{frame_id}/display | | FrameDisplayView | 404 unknown_frame |
| GET | /v1/operator/frames/{frame_id}/power | | FramePowerView | 404 unknown_frame |
| POST | /v1/operator/frames/{frame_id}/power-tests | | 202 PowerTestAccepted | 404 unknown_frame, 409 frame_unbound |
| DELETE | /v1/operator/frames/{frame_id}/power-tests | | 204 | 404 unknown_frame |
| PUT | /v1/operator/displays/{display_id}/power-settings | DisplayPowerSettings | DisplayPowerSettings | 404 unknown_display |
| GET | /v1/operator/frames/{frame_id}/picture | | FramePictureView | 404 unknown_frame |
| POST | /v1/operator/frames/{frame_id}/picture-sessions | | 201 PictureSessionView | 404 unknown_frame, 409 frame_unbound, no_display, session_open |
| POST | /v1/operator/picture-sessions/{session_id}/hardware-edits | HardwareEdit | PictureSequence | 404 unknown_session, 410 session_ended, 409 stale_sequence, not_offered, 422 out_of_range |
| POST | /v1/operator/picture-sessions/{session_id}/software-edits | SoftwareEdit | PictureSequence | 404, 410, 409 stale_sequence, 422 out_of_range |
| POST | /v1/operator/picture-sessions/{session_id}/lut-edits | LutEdit | PictureSequence | 404, 410, 409 stale_sequence, 422 unknown_lut |
| POST | /v1/operator/picture-sessions/{session_id}/signal-range-edits | SignalRangeEdit | PictureSequence | 404 unknown_session, 410 session_ended, 409 stale_sequence, 422 (a value outside SignalRange) |
| POST | /v1/operator/picture-sessions/{session_id}/test-pattern-edits | TestPatternEdit | PictureSequence | 404 unknown_session, 410 session_ended, 409 stale_sequence, 422 (a value outside TestPattern) |
| POST | /v1/operator/picture-sessions/{session_id}/keepalive | | PictureSessionView | 404, 410 |
| POST | /v1/operator/picture-sessions/{session_id}/done | | 204 | 404, 410, 409 not_presented_yet |
| DELETE | /v1/operator/picture-sessions/{session_id} | | 204 (Revert) | 404, 410 |
| POST | /v1/operator/frames/{frame_id}/picture-copies | PictureCopyRequest | tuple[PictureCopyResult, ...] | 404 unknown_frame, 409 no_display |

Every route takes the operator `admin` dependency, as every /v1/operator route does. A refusal
answers {"error": code} with its status, the shape apiWrite.js reads. create_app mounts them through
`mount_display_routes`: GET display (slice C1) and the Power tab's four, `_mount_power_routes`
(slice C3). POST is Test: turn off; DELETE is Turn on, which ends the test so the request underneath
takes over (owner q1, 2026-10-10). The Picture tab's eleven (roadmap 1c's Live model; decision 0022,
the picture split, slice S2: one edit route per group; the print look's LUT choice its own, since
it names a LUT where the software edit sets a number; display tuning's signal range and test
pattern, decision 0024, one each) are `mount_picture_routes`, which create_app
mounts too; picture refusals (central/displays/picture.py PictureRefused) answer the same way.
"""
from __future__ import annotations

from typing import Any, Final
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from central.displays.picture import PictureRefused
from central.displays.ports import (
    DisplayCommands,
    DisplayQueries,
    DisplayRefused,
    PictureCommands,
    PictureQueries,
)
from central.displays.views import (
    DisplayPowerSettings,
    FrameDisplayView,
    FramePictureView,
    FramePowerView,
    HardwareEdit,
    LutEdit,
    PictureCopyRequest,
    PictureCopyResult,
    PictureSequence,
    PictureSessionView,
    PowerTestAccepted,
    SignalRangeEdit,
    SoftwareEdit,
    TestPatternEdit,
)
from central.operator_auth import OPERATOR_PREFIX
from contracts.models import Identifier

FRAMES: Final = OPERATOR_PREFIX + "frames/"
DISPLAYS: Final = OPERATOR_PREFIX + "displays/"
PICTURE_SESSIONS: Final = OPERATOR_PREFIX + "picture-sessions/"


def mount_display_routes(app: FastAPI, *, admin: Any, queries: DisplayQueries, commands: DisplayCommands) -> None:
    """Declare the routes above on `app`; DisplayRefused becomes {"error": code} at its status."""

    @app.exception_handler(DisplayRefused)
    async def refused(request: Request, exc: DisplayRefused) -> JSONResponse:
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    @app.get(FRAMES + "{frame_id}/display", dependencies=[Depends(admin)], response_model=FrameDisplayView)
    def frame_display(frame_id: Identifier) -> FrameDisplayView:
        return queries.frame_display(frame_id)

    _mount_power_routes(app, admin=admin, queries=queries, commands=commands)


def _mount_power_routes(app: FastAPI, *, admin: Any, queries: DisplayQueries, commands: DisplayCommands) -> None:
    """The Power tab's four routes (slice C3)."""

    @app.get(FRAMES + "{frame_id}/power", dependencies=[Depends(admin)], response_model=FramePowerView)
    def frame_power(frame_id: Identifier) -> FramePowerView:
        return queries.frame_power(frame_id)

    @app.post(FRAMES + "{frame_id}/power-tests", dependencies=[Depends(admin)], status_code=202,
              response_model=PowerTestAccepted)
    def power_test(frame_id: Identifier) -> PowerTestAccepted:
        return commands.request_power_test(frame_id)

    @app.delete(FRAMES + "{frame_id}/power-tests", dependencies=[Depends(admin)], status_code=204,
                response_class=Response)
    def end_power_test(frame_id: Identifier) -> Response:
        commands.end_power_test(frame_id)
        return Response(status_code=204)

    @app.put(DISPLAYS + "{display_id}/power-settings", dependencies=[Depends(admin)],
             response_model=DisplayPowerSettings)
    def power_settings(display_id: UUID, request: DisplayPowerSettings) -> DisplayPowerSettings:
        return commands.set_power_settings(display_id, request)


def mount_picture_routes(app: FastAPI, *, admin: Any, queries: PictureQueries, commands: PictureCommands) -> None:
    """The Picture tab's eleven routes, mounted by create_app beside
    `mount_display_routes` (whose handler answers DisplayRefused); PictureRefused answers the same way."""

    @app.exception_handler(PictureRefused)
    async def picture_refused(request: Request, exc: PictureRefused) -> JSONResponse:
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    @app.get(FRAMES + "{frame_id}/picture", dependencies=[Depends(admin)], response_model=FramePictureView)
    def frame_picture(frame_id: Identifier) -> FramePictureView:
        return queries.frame_picture(frame_id)

    @app.post(FRAMES + "{frame_id}/picture-sessions", dependencies=[Depends(admin)], status_code=201,
              response_model=PictureSessionView)
    def begin_picture(frame_id: Identifier) -> PictureSessionView:
        return commands.begin_picture(frame_id)

    @app.post(PICTURE_SESSIONS + "{session_id}/hardware-edits", dependencies=[Depends(admin)],
              response_model=PictureSequence)
    def edit_hardware(session_id: UUID, request: HardwareEdit) -> PictureSequence:
        return commands.edit_hardware(session_id, request)

    @app.post(PICTURE_SESSIONS + "{session_id}/software-edits", dependencies=[Depends(admin)],
              response_model=PictureSequence)
    def edit_software(session_id: UUID, request: SoftwareEdit) -> PictureSequence:
        return commands.edit_software(session_id, request)

    @app.post(PICTURE_SESSIONS + "{session_id}/lut-edits", dependencies=[Depends(admin)],
              response_model=PictureSequence)
    def edit_lut(session_id: UUID, request: LutEdit) -> PictureSequence:
        return commands.edit_lut(session_id, request)

    @app.post(PICTURE_SESSIONS + "{session_id}/signal-range-edits", dependencies=[Depends(admin)],
              response_model=PictureSequence)
    def edit_signal_range(session_id: UUID, request: SignalRangeEdit) -> PictureSequence:
        return commands.edit_signal_range(session_id, request)

    @app.post(PICTURE_SESSIONS + "{session_id}/test-pattern-edits", dependencies=[Depends(admin)],
              response_model=PictureSequence)
    def edit_test_pattern(session_id: UUID, request: TestPatternEdit) -> PictureSequence:
        return commands.edit_test_pattern(session_id, request)

    @app.post(PICTURE_SESSIONS + "{session_id}/keepalive", dependencies=[Depends(admin)],
              response_model=PictureSessionView)
    def keep_picture_alive(session_id: UUID) -> PictureSessionView:
        return commands.keep_picture_alive(session_id)

    @app.post(PICTURE_SESSIONS + "{session_id}/done", dependencies=[Depends(admin)], status_code=204,
              response_class=Response)
    def picture_done(session_id: UUID) -> Response:
        commands.picture_done(session_id)
        return Response(status_code=204)

    @app.delete(PICTURE_SESSIONS + "{session_id}", dependencies=[Depends(admin)], status_code=204,
                response_class=Response)
    def revert_picture(session_id: UUID) -> Response:
        commands.revert_picture(session_id)
        return Response(status_code=204)

    @app.post(FRAMES + "{frame_id}/picture-copies", dependencies=[Depends(admin)],
              response_model=tuple[PictureCopyResult, ...])
    def copy_picture(frame_id: Identifier, request: PictureCopyRequest) -> tuple[PictureCopyResult, ...]:
        return commands.copy_picture(frame_id, request)

