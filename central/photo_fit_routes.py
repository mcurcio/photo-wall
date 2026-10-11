"""The photo fit operator routes (follow-ups 2026-10-11, slices B1 and B2; run ledger
.claude/runs/photo-5a.md "Follow-ups (owner 2026-10-11)").

| Method | Path | Body | Answer | Refusals | Slice |
|---|---|---|---|---|---|
| GET | /v1/operator/settings/photo-fit | | `FitSettingsView` | | B1 |
| PUT | /v1/operator/settings/photo-fit | `FitSettings` (complete) | `FitSettingsView` | 422 (a value outside its range) | B1 |
| POST | /v1/operator/photo-fit/analysis | `FitAnalysisRequest` | `FitAnalysis` | 404 `source_unknown`, `draft_unknown`, `frame_unknown`; 422 | B2 |

Every route takes the operator `admin` dependency; a refusal answers {"error": code} at its
status, as `apiWrite.js` reads.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from fastapi import FastAPI

from central.wall_plan.fit_analysis import FitAnalysis, FitAnalysisRequest
from central.wall_plan.ports import FitSettingsStore

SETTINGS_PATH: Final = "/v1/operator/settings/photo-fit"
ANALYSIS_PATH: Final = "/v1/operator/photo-fit/analysis"


def mount_photo_fit_routes(app: FastAPI, *, admin: Any, settings: FitSettingsStore,
                           analysis: Callable[[FitAnalysisRequest], FitAnalysis]) -> None:
    """Mount the routes above. `analysis` is `central.wall_plan.fit_analysis.analyse` bound to
    the PostgreSQL reader and the saved settings (wired in `central.app.create_app`);
    `AnalysisRefused` becomes {"error": code} at its status."""
    raise NotImplementedError


__all__ = ["ANALYSIS_PATH", "SETTINGS_PATH", "mount_photo_fit_routes"]
