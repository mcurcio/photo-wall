"""The display line's canonical JSON codec for frozen wire dataclasses (decision 0022; slice S1 of
the picture split moves these here from contracts/node_output.py's private helpers, and both
contracts/node_output.py and contracts/node_picture.py use them). Stdlib and contracts only: the
Pi runs it.

Rules this module owns (one home each):
- **Canonical bytes:** sorted keys, no spaces, UTF-8, no NaN; a dataclass is an object of its
  field names, a tuple a list, a Mapping an object of its plain values, an enum its value, None
  null. A digest of the bytes is stable for an unchanged value.
- **Strict decoding:** contracts.strict_json rules (no duplicate keys, no NaN), the byte cap
  checked before parsing, every object holding exactly its dataclass's field names; any failure
  is ValueError(code) with the caller's code.
- **The shared checks** the wire dataclasses' `__post_init__` use: `require`, `whole`,
  `printable`.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import fields, is_dataclass
from enum import Enum, StrEnum
from typing import Any, TypeVar

from contracts.strict_json import loads_object

_Wire = TypeVar("_Wire")


def require(condition: bool, code: str) -> None:
    """ValueError(code) unless `condition`."""
    if not condition:
        raise ValueError(code)


def whole(value: object, minimum: int, maximum: int = 2**63 - 1) -> bool:
    """An int (never a bool) in minimum..maximum."""
    return type(value) is int and minimum <= value <= maximum


def printable(value: object, shortest: int, longest: int) -> bool:
    """A str of shortest..longest printable characters."""
    return isinstance(value, str) and shortest <= len(value) <= longest and value.isprintable()


def plain(value: object) -> object:
    """`value` as JSON types (the module's canonical-bytes rule, before encoding)."""
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [plain(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


def encode(value: object, limit: int, too_large: str) -> bytes:
    """Canonical bytes of `value`; ValueError(too_large) past `limit` bytes."""
    raw = json.dumps(plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                     allow_nan=False).encode()
    if len(raw) > limit:
        raise ValueError(too_large)
    return raw


def decode(raw: bytes, limit: int, cls: type[_Wire], build: Callable[[dict[str, Any]], _Wire],
           code: str) -> _Wire:
    """The `cls` that `raw` encodes, built by `build` from its checked fields; ValueError(code) for
    anything else (too large, not strict JSON, a missing or unknown field, a wrong type or value)."""
    value = loads_object(raw, max_bytes=limit) if isinstance(raw, bytes) else None
    try:
        if value is None:
            raise ValueError(code)
        return build(fields_of(cls, value))
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError(code) from error


def fields_of(cls: type, value: object) -> dict[str, Any]:
    """`value` as `cls`'s keyword arguments: an object holding exactly its field names;
    ValueError(cls.__name__) otherwise."""
    if not isinstance(value, dict) or set(value) != {field.name for field in fields(cls)}:
        raise ValueError(cls.__name__)
    return value


def list_of(value: object) -> list[object]:
    """`value` when it is a JSON array; ValueError("list") otherwise."""
    if not isinstance(value, list):
        raise ValueError("list")
    return value


def object_of(value: object) -> dict[str, object]:
    """`value` when it is a JSON object; ValueError("object") otherwise (a Mapping field's wire form)."""
    if not isinstance(value, dict):
        raise ValueError("object")
    return value


def optional(kind: type[StrEnum], value: object) -> Any:
    """None for null, else `kind(value)` (ValueError for a value outside it)."""
    return None if value is None else kind(value)


__all__ = ["decode", "encode", "fields_of", "list_of", "object_of", "optional", "plain", "printable", "require",
           "whole"]
