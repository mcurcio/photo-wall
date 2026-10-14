"""A CI leg run as parallel shards (checks.yml's unit and browser legs, the owner's ~4 min per leg).

A run without PHOTO_WALL_TEST_SHARD is whole. A test belongs to the shard its function's name
hashes to (its parametrised cases stay together), so every process agrees without talking: each
xdist worker collects the same tests, and the controller, which collects nothing, still knows
which named checks are its shard's (tests/browser/conftest.py's evidence). The shards together
run every collected test exactly once. A name's hash, not its measured time: no durations file to
keep current, and over thousands of names the legs come out within a few seconds of each other.
"""

import hashlib
import os
import re

import pytest

SHARD_VARIABLE = "PHOTO_WALL_TEST_SHARD"


def shard() -> tuple[int, int] | None:
    """(index, count) from PHOTO_WALL_TEST_SHARD "index/count" (index from 1), or None."""
    text = os.environ.get(SHARD_VARIABLE)
    if not text:
        return None
    match = re.fullmatch(r"([1-9][0-9]*)/([1-9][0-9]*)", text)
    if match is None or int(match[1]) > int(match[2]):
        raise pytest.UsageError(f"{SHARD_VARIABLE} must be index/count, got {text!r}")
    return int(match[1]), int(match[2])


def in_shard(name: str, selected: tuple[int, int] | None) -> bool:
    if selected is None:
        return True
    index, count = selected
    return int(hashlib.sha256(name.encode()).hexdigest(), 16) % count == index - 1


def select_shard(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect every test outside this run's shard."""
    selected = shard()
    if selected is None:
        return
    kept, dropped = [], []
    for item in items:
        (kept if in_shard(getattr(item, "originalname", item.name), selected) else dropped).append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept
