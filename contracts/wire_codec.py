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

Interface only (the picture split's milestone commit): bodies land in slice S1.
"""
from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any, TypeVar

_Wire = TypeVar("_Wire")


def require(condition: bool, code: str) -> None:
    """ValueError(code) unless `condition`."""
    raise NotImplementedError("picture split S1")


def whole(value: object, minimum: int, maximum: int = 2**63 - 1) -> bool:
    """An int (never a bool) in minimum..maximum."""
    raise NotImplementedError("picture split S1")


def printable(value: object, shortest: int, longest: int) -> bool:
    """A str of shortest..longest printable characters."""
    raise NotImplementedError("picture split S1")


def plain(value: object) -> object:
    """`value` as JSON types (the module's canonical-bytes rule, before encoding)."""
    raise NotImplementedError("picture split S1")


def encode(value: object, limit: int, too_large: str) -> bytes:
    """Canonical bytes of `value`; ValueError(too_large) past `limit` bytes."""
    raise NotImplementedError("picture split S1")


def decode(raw: bytes, limit: int, cls: type[_Wire], build: Callable[[dict[str, Any]], _Wire],
           code: str) -> _Wire:
    """The `cls` that `raw` encodes, built by `build` from its checked fields; ValueError(code) for
    anything else (too large, not strict JSON, a missing or unknown field, a wrong type or value)."""
    raise NotImplementedError("picture split S1")


def fields_of(cls: type, value: object) -> dict[str, Any]:
    """`value` as `cls`'s keyword arguments: an object holding exactly its field names;
    ValueError(cls.__name__) otherwise."""
    raise NotImplementedError("picture split S1")


def list_of(value: object) -> list[object]:
    """`value` when it is a JSON array; ValueError("list") otherwise."""
    raise NotImplementedError("picture split S1")


def object_of(value: object) -> dict[str, object]:
    """`value` when it is a JSON object; ValueError("object") otherwise (a Mapping field's wire form)."""
    raise NotImplementedError("picture split S1")


def optional(kind: type[StrEnum], value: object) -> Any:
    """None for null, else `kind(value)` (ValueError for a value outside it)."""
    raise NotImplementedError("picture split S1")


__all__ = ["decode", "encode", "fields_of", "list_of", "object_of", "optional", "plain", "printable", "require",
           "whole"]
