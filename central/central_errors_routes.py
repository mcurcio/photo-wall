"""Central's errors' routes (a composition-root module like power_routes.py), the shapes
central/central_errors/views.py's:

| Method | Path | Answer |
|---|---|---|
| GET | /v1/operator/central-errors | CentralErrorsView (Needs attention's source rows) |
| GET | /v1/operator/frames/{frame_id}/central-error | FrameImpedimentView (the Frame page's "Central's error") |

The routes take the operator `admin` dependency. A store failure answers 500: never "nothing needs
attention".
"""
from __future__ import annotations

from typing import Any, Final

from fastapi import Depends, FastAPI

from central.central_errors.ports import CentralErrors
from central.central_errors.views import CentralErrorsView, FrameImpedimentView
from central.operator_auth import OPERATOR_PREFIX
from contracts.identifiers import TargetIdentifier

CENTRAL_ERRORS: Final = OPERATOR_PREFIX + "central-errors"
FRAME_CENTRAL_ERROR: Final = OPERATOR_PREFIX + "frames/{frame_id}/central-error"


def mount_central_error_routes(app: FastAPI, *, admin: Any, errors: CentralErrors) -> None:
    """Declare the routes above on `app`."""

    @app.get(CENTRAL_ERRORS, dependencies=[Depends(admin)], response_model=CentralErrorsView)
    def central_errors() -> CentralErrorsView:
        return errors.in_doubt()

    @app.get(FRAME_CENTRAL_ERROR, dependencies=[Depends(admin)], response_model=FrameImpedimentView)
    def frame_central_error(frame_id: TargetIdentifier) -> FrameImpedimentView:
        return errors.impeded(frame_id)


__all__ = ["CENTRAL_ERRORS", "FRAME_CENTRAL_ERROR", "mount_central_error_routes"]
