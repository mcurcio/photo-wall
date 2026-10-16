"""The identifier rules every wire shape shares, in their own low module so a contract that needs only
an id (the Node's state keys, Central on its hub) imports no Frame geometry. contracts/models.py
re-exports them under their historical names.
"""
from __future__ import annotations

from typing import Annotated

from pydantic import Field

# The API identifier rule (path parameters, Scene/Program/Source ids). The console's
# IDENTIFIER_PATTERN (authoring.js) is pinned equal to it by a test.
IDENTIFIER_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"
Identifier = Annotated[str, Field(pattern=rf"^{IDENTIFIER_PATTERN}$")]
# The one usable Frame/Actuator id rule: a Scene reaches a target only as
# "<kind>:<id>" (central/runtime.py `Target`), so an id has no ':' and at most 96
# characters. New Frames are created under it (central/registry.py `FrameCreate`);
# the console's FRAME_ID_PATTERN is pinned equal to it by a test.
TARGET_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}"
TargetIdentifier = Annotated[str, Field(pattern=rf"^{TARGET_ID_PATTERN}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Instant = Annotated[float, Field(allow_inf_nan=False)]

__all__ = ["IDENTIFIER_PATTERN", "TARGET_ID_PATTERN", "Digest", "Identifier", "Instant", "TargetIdentifier"]
