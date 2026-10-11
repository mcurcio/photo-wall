"""The media worker's failure vocabulary; never part of the Player protocol.

The photo library's own values (queries, assets, problems) live in central.photo_library.model.
"""

from __future__ import annotations

import re
from typing import Literal

Status = Literal["unavailable", "permission", "incompatible"]
Kind = Literal["image", "video"]


class MediaError(Exception):
    """Only bounded codes cross the worker boundary; no chained transport exception."""

    def __init__(self, code: str, status: Status = "unavailable") -> None:
        if not re.fullmatch(r"[a-z_]{1,64}", code):
            raise ValueError("invalid media failure code")
        self.code, self.status = code, status
        super().__init__(code)
