"""The operator's configuration snapshot reads (owner, 2026-10-13: "configuration snapshots";
migration 096; a composition-root module like why_routes.py): the agent's and the console's one read
contract, `central/tuning_history/ports.py` TuningHistoryQueries. One route per question:

| Method | Path | Query | Answer | Refusals |
|---|---|---|---|---|
| GET | /v1/operator/displays/{display_id}/picture-history | before (ISO 8601 with zone, exclusive), limit (1..500, default 100) | PictureHistoryView | 404 unknown_display, 422 bad_time, bad_range |
| GET | /v1/operator/frames/{frame_id}/position-history | before, limit (as above) | PositionHistoryView | 404 unknown_frame, 422 bad_time, bad_range |
| GET | /v1/operator/frames/{frame_id}/tuning-snapshot | at (ISO 8601 with zone, required) | TuningSnapshotView | 404 unknown_frame, 422 bad_time |

Every route takes the operator `admin` dependency. `before`, `limit` and `at` are read as text and
judged by central/tuning_history/model.py (`history_page`, `utc_instant`), so every refusal is
HistoryRefused, answered as {"error": code} at its status. The two Restores are their owners' routes
(central/display_routes.py: POST /v1/operator/picture-sessions/{session_id}/restores;
central/fleet/node_routes.py: POST /v1/operator/frames/{frame_id}/calibration-trials/{trial_id}/restores).
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from central.displays.views import PictureHistoryView
from central.operator_auth import OPERATOR_PREFIX
from central.tuning_history.model import HistoryRefused, history_page
from central.tuning_history.ports import TuningHistoryQueries


def mount_tuning_history_routes(app: FastAPI, *, admin: Any, queries: TuningHistoryQueries) -> None:
    """Declare the routes above on `app`; HistoryRefused becomes {"error": code} at its status."""

    @app.exception_handler(HistoryRefused)
    async def refused(request: Request, exc: HistoryRefused) -> JSONResponse:
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    @app.get(OPERATOR_PREFIX + "displays/{display_id}/picture-history", dependencies=[Depends(admin)],
             response_model=PictureHistoryView)
    def picture_history(display_id: UUID, before: str | None = None, limit: int | None = None) -> PictureHistoryView:
        return queries.picture_history(display_id, history_page(before, limit))


__all__ = ["mount_tuning_history_routes"]
