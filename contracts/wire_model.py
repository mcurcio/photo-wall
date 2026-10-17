"""The base of every pydantic wire model in `contracts`: frozen, closed to unknown fields, no NaN or
infinity, and the one strict decoder of such a model. One home, so contracts/models.py and
contracts/frame_lighting.py (which models.py imports) share it without importing each other."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, ValidationError

from contracts.strict_json import loads_object


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def decoded[M: Model](cls: type[M], raw: bytes, limit: int, code: str) -> M:
    """`cls` from `raw`: contracts.strict_json's rules within `limit` bytes (no duplicate keys, no
    NaN, the byte cap before parsing), then pydantic's strict JSON validation; ValueError(code) for
    anything else."""
    if not isinstance(raw, bytes) or loads_object(raw, max_bytes=limit) is None:
        raise ValueError(code)
    try:
        return cls.model_validate_json(raw, strict=True)
    except ValidationError as error:
        raise ValueError(code) from error


__all__ = ["Model", "decoded"]
