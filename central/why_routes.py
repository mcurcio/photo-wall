"""The operator's Why routes (roadmap 2b E6; Power and the week: slice T the Frame's Why, slice S3
the week; a composition-root module like display_routes.py). One route per question:

| Method | Path | Body | Answer | Refusals |
|---|---|---|---|---|
| GET | /v1/operator/frames/{frame_id}/why | | FrameWhy | 404 unknown_frame |
| GET | /v1/operator/week?first_day=YYYY-MM-DD | | Week | 422 (FastAPI) for a malformed date |

Every route takes the operator `admin` dependency. WhyRefused answers {"error": code} at its status.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from central.why.ports import WhyQueries


def mount_why_routes(app: FastAPI, *, admin: Any, queries: WhyQueries) -> None:
    """Declare the routes above on `app`; WhyRefused becomes {"error": code} at its status."""
    raise NotImplementedError


__all__ = ["mount_why_routes"]
