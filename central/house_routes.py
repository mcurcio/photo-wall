"""The operator's Settings › House routes (roadmap 2b A4; Power and the week, slice S3; a
composition-root module like display_routes.py). One route per question:

| Method | Path | Body | Answer | Refusals |
|---|---|---|---|---|
| GET | /v1/operator/house | | HouseSettings | |
| PUT | /v1/operator/house | HouseSettingsDraft | HouseSettings | 422 unknown-zone |

Every route takes the operator `admin` dependency. HouseRefused answers {"error": code} at its
status, the shape apiWrite.js reads.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from central.house_time.ports import HouseCommands, HouseQueries


def mount_house_routes(app: FastAPI, *, admin: Any, queries: HouseQueries, commands: HouseCommands) -> None:
    """Declare the routes above on `app`; HouseRefused becomes {"error": code} at its status."""
    raise NotImplementedError


__all__ = ["mount_house_routes"]
