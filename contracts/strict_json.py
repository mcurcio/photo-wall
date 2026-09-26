"""Strict JSON objects for device wire formats; stdlib only, so it runs in the initramfs."""

import json
import math


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):  # 1e999 parses to Infinity without passing parse_constant
        raise ValueError("non-finite number")
    return value


def _constant(name: str) -> object:
    raise ValueError(f"{name} is not JSON")


def loads_object(data: bytes, *, max_bytes: int) -> dict[str, object] | None:
    """A UTF-8 JSON object of at most `max_bytes`, with no duplicate keys and no NaN or
    Infinity. Anything else returns None."""
    if len(data) > max_bytes:
        return None
    try:
        # Decode first: json.loads(bytes) would also accept UTF-16 and UTF-32.
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique,
                           parse_float=_finite, parse_constant=_constant)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None
