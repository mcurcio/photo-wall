"""The base of every pydantic wire model in `contracts`: frozen, closed to unknown fields, no NaN or
infinity. One home, so contracts/models.py and contracts/frame_lighting.py (which models.py
imports) share it without importing each other."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


__all__ = ["Model"]
