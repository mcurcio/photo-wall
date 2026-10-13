"""The operator's calibration target route (decision 0026; a composition-root module like
tuning_history_routes.py): the paper target to print. The screen target is a test pattern
(central/display_routes.py's test-pattern edits); what a photo of both shows is read by the agent's
reader (scripts/calibration_target/), which reads the sent entry its QR names over
central/tuning_history_routes.py.

| Method | Path | Query | Answer | Refusals |
|---|---|---|---|---|
| GET | /v1/operator/calibration-target/paper.pdf | paper (`letter` or `a4`, required) | application/pdf, central/displays/calibration_paper.py `paper_pdf`; Content-Disposition attachment, filename photo-wall-calibration-target-<paper>.pdf; Cache-Control private, no-store | 422 bad_paper |

The route takes the operator `admin` dependency; a refusal is answered as {"error": code} at its
status. `paper` is read as text and judged here (PaperSize's values), so every refusal is
`bad_paper`, never FastAPI's own 422 shape.
"""
from __future__ import annotations

from typing import Any, Final

from fastapi import FastAPI

PAPER_PDF_PATH: Final = "calibration-target/paper.pdf"     # under OPERATOR_PREFIX


def mount_calibration_target_routes(app: FastAPI, *, admin: Any) -> None:
    """Declare the route above on `app`."""
    raise NotImplementedError("calibration targets: slice K2")


__all__ = ["PAPER_PDF_PATH", "mount_calibration_target_routes"]
