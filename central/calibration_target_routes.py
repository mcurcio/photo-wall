"""The operator's calibration target route (decision 0026; a composition-root module like
tuning_history_routes.py): the paper target to print. The screen target is a test pattern
(central/display_routes.py's test-pattern edits); what a photo of both shows is read by the agent's
reader (scripts/calibration_target/), which reads the sent entry its QR names over
central/tuning_history_routes.py.

| Method | Path | Query | Answer | Refusals |
|---|---|---|---|---|
| GET | /v1/operator/calibration-target/paper.pdf | paper (`letter`, `a4` or `4x6`, required) | application/pdf, the committed sheet's bytes (central/displays/calibration_paper.py `sheet_path`); Content-Disposition attachment, filename `sheet_name`; Cache-Control private, no-store | 422 bad_paper |

The route takes the operator `admin` dependency; a refusal is answered as {"error": code} at its
status. `paper` is read as text and judged here (PaperSize's values), so every refusal is
`bad_paper`, never FastAPI's own 422 shape. The sheets are files committed beside Central and
drawn at build time (scripts/calibration_boards.py; the README links the same files): the route
serves their bytes as they are, so the console's print link, the README and a curl get one sheet,
and it keeps the operator guard and the download name.
"""
from __future__ import annotations

from typing import Any, Final

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse, Response

from central.displays.calibration_paper import PDF_MEDIA_TYPE, sheet_name, sheet_path
from central.operator_auth import OPERATOR_PREFIX
from contracts.calibration_target import PaperSize

PAPER_PDF_PATH: Final = "calibration-target/paper.pdf"     # under OPERATOR_PREFIX


def mount_calibration_target_routes(app: FastAPI, *, admin: Any) -> None:
    """Declare the route above on `app`."""

    @app.get(OPERATOR_PREFIX + PAPER_PDF_PATH, dependencies=[Depends(admin)], response_class=Response,
             responses={200: {"content": {PDF_MEDIA_TYPE: {}}}})
    def paper_target(paper: str | None = None) -> Response:
        try:
            sheet = PaperSize(paper)
        except ValueError:
            return JSONResponse({"error": "bad_paper"}, status_code=422)
        return Response(sheet_path(sheet).read_bytes(), media_type=PDF_MEDIA_TYPE, headers={
            "Content-Disposition": f"attachment; filename={sheet_name(sheet)}",
            "Cache-Control": "private, no-store"})


__all__ = ["PAPER_PDF_PATH", "mount_calibration_target_routes"]
