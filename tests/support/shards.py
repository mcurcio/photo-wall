"""A CI leg run as parallel shards (checks.yml's unit, browser and catalog legs, the owner's ~4 min per leg).

A run without PHOTO_WALL_TEST_SHARD is whole. A test belongs to the shard its function's name
hashes to (its parametrised cases stay together), so every process agrees without talking: each
xdist worker collects the same tests, and the controller, which collects nothing, still knows
which named checks are its shard's (tests/browser/conftest.py's evidence). The shards together
run every collected test exactly once. A name's hash, not its measured time: no durations file to
keep current, and over thousands of names the legs come out within a few seconds of each other.

A test marked `shard_each_case` is the exception: each parametrised case hashes by its own name
(`test[case]`), for a test whose cases are the whole leg (the console catalog walks every story
in one test). Only a test no shard-scoped evidence names may take it: the browser evidence counts
a named check by its function's name.
"""

import hashlib
import os
import re

import pytest

SHARD_VARIABLE = "PHOTO_WALL_TEST_SHARD"
EACH_CASE = "shard_each_case"


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


def shard_name(item: pytest.Item) -> str:
    """The name a test's shard is hashed from: its function's, or its case's when marked."""
    if item.get_closest_marker(EACH_CASE) is not None:
        return item.name
    return getattr(item, "originalname", item.name)


def select_shard(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect every test outside this run's shard."""
    selected = shard()
    if selected is None:
        return
    kept, dropped = [], []
    for item in items:
        (kept if in_shard(shard_name(item), selected) else dropped).append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept
