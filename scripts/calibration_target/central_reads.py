"""Central's edge for the reader (decision 0026): the one read it needs, never a write.

- The sent entry the code names: the operator route GET /v1/operator/players/{player_id}/outputs/
  {output_id}/picture-changes/{picture_change} (central/tuning_history_routes.py), with the admin
  token, decoded as Central's own PictureEntryView (one shape for the console and the agent).

The post-software target goes through the Display's own steps only (decision 0028), so no LUT's
table is read any more.
"""
from __future__ import annotations

from typing import Final, Protocol

import httpx

from central.displays.views import PictureEntryView
from contracts.calibration_target import TargetCode


class CentralReads(Protocol):
    def sent_entry(self, code: TargetCode) -> PictureEntryView | None:
        """The sent entry `code` names; None when Central answers 404 unknown_entry."""
        ...


class HttpCentralReads:
    """CentralReads over Central's operator API (httpx). Any other answer than 200 or 404 from
    Central is RuntimeError naming its status; the admin token comes from the environment the
    command is run in (`op run`), never from arguments that land in a shell's history:
    PHOTO_WALL_ADMIN_TOKEN."""

    def __init__(self, base_url: str, admin_token: str) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=TIMEOUT_SECONDS,
                                    headers={"Authorization": f"Bearer {admin_token}"})

    def sent_entry(self, code: TargetCode) -> PictureEntryView | None:
        answer = self._client.get(f"/v1/operator/players/{code.player_id}/outputs/{code.output_id}"
                                  f"/picture-changes/{code.picture_change}")
        if answer.status_code == 404:
            return None
        if answer.status_code != 200:
            raise RuntimeError(f"central answered {answer.status_code}")
        return PictureEntryView.model_validate(answer.json())


TIMEOUT_SECONDS: Final = 10.0


__all__ = ["CentralReads", "HttpCentralReads"]
