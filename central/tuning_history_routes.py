"""The operator's configuration snapshot reads (owner, 2026-10-13: "configuration snapshots";
migration 096; a composition-root module like why_routes.py): the agent's and the console's one read
contract, `central/tuning_history/ports.py` TuningHistoryQueries. One route per question:

| Method | Path | Query | Answer | Refusals |
|---|---|---|---|---|
| GET | /v1/operator/displays/{display_id}/picture-history | before (ISO 8601 with zone, exclusive), limit (1..500, default 100) | PictureHistoryView | 404 unknown_display, 422 bad_time, bad_range |
| GET | /v1/operator/frames/{frame_id}/position-history | before, limit (as above) | PositionHistoryView | 404 unknown_frame, 422 bad_time, bad_range |
| GET | /v1/operator/frames/{frame_id}/tuning-snapshot | at (ISO 8601 with zone, required) | TuningSnapshotView | 404 unknown_frame, 422 bad_time |
| GET | /v1/operator/players/{player_id}/outputs/{output_id}/picture-changes/{picture_change} | none | PictureEntryView | 404 unknown_entry |

Every route takes the operator `admin` dependency. `before`, `limit` and `at` are read as text and
judged by central/tuning_history/bounds.py (`history_page`, `utc_instant`); `picture_change` is read
as text too, and anything but a canonical decimal from 1 to PostgreSQL's BIGINT maximum names no
entry (`_picture_change`). Every refusal is HistoryRefused, answered as {"error": code} at its status. The two Restores are their owners' routes
(central/display_routes.py: POST /v1/operator/picture-sessions/{session_id}/restores;
central/fleet/node_routes.py: POST /v1/operator/frames/{frame_id}/calibration-trials/{trial_id}/restores).
"""
from __future__ import annotations

import re
from typing import Any, Final
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from central.displays.views import PictureEntryView, PictureHistoryView
from central.fleet.position_views import PositionHistoryView
from central.operator_auth import OPERATOR_PREFIX
from central.tuning_history.bounds import HistoryRefused, history_page, utc_instant
from central.tuning_history.ports import TuningHistoryQueries
from central.tuning_history.views import TuningSnapshotView
from contracts.models import Identifier

_CANONICAL_CHANGE: Final = re.compile(r"[1-9][0-9]*")
_BIGINT_MAX: Final = 2**63 - 1      # picture_sent_entries.picture_change is BIGINT


def _picture_change(text: str) -> int:
    """`text` as a picture change: a canonical ASCII decimal from 1 to _BIGINT_MAX, else
    HistoryRefused("unknown_entry", 404) (no port has such a change)."""
    if _CANONICAL_CHANGE.fullmatch(text) is None or int(text) > _BIGINT_MAX:
        raise HistoryRefused("unknown_entry", 404)
    return int(text)


def mount_tuning_history_routes(app: FastAPI, *, admin: Any, queries: TuningHistoryQueries) -> None:
    """Declare the routes above on `app`; HistoryRefused becomes {"error": code} at its status."""

    @app.exception_handler(HistoryRefused)
    async def refused(request: Request, exc: HistoryRefused) -> JSONResponse:
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    @app.get(OPERATOR_PREFIX + "displays/{display_id}/picture-history", dependencies=[Depends(admin)],
             response_model=PictureHistoryView)
    def picture_history(display_id: UUID, before: str | None = None, limit: int | None = None) -> PictureHistoryView:
        return queries.picture_history(display_id, history_page(before, limit))

    @app.get(OPERATOR_PREFIX + "frames/{frame_id}/position-history", dependencies=[Depends(admin)],
             response_model=PositionHistoryView)
    def position_history(frame_id: Identifier, before: str | None = None,
                         limit: int | None = None) -> PositionHistoryView:
        return queries.position_history(frame_id, history_page(before, limit))

    @app.get(OPERATOR_PREFIX + "frames/{frame_id}/tuning-snapshot", dependencies=[Depends(admin)],
             response_model=TuningSnapshotView)
    def tuning_snapshot(frame_id: Identifier, at: str | None = None) -> TuningSnapshotView:
        if at is None:
            raise HistoryRefused("bad_time", 422)
        return queries.snapshot_at(frame_id, utc_instant(at))

    @app.get(OPERATOR_PREFIX + "players/{player_id}/outputs/{output_id}/picture-changes/{picture_change}",
             dependencies=[Depends(admin)], response_model=PictureEntryView)
    def sent_picture_entry(player_id: str, output_id: str, picture_change: str) -> PictureEntryView:
        return queries.sent_picture_entry(player_id, output_id, _picture_change(picture_change))


__all__ = ["mount_tuning_history_routes"]
