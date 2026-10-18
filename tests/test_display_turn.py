"""The Display turn's vocabulary and its one mapping to Wayland's output transform."""
from __future__ import annotations

import pytest

from contracts.display_turn import TURNS, hanging, turn_of, wayland_transform


def test_every_turn_round_trips_through_its_wayland_transform():
    assert [wayland_transform(turn) for turn in TURNS] == [0, 1, 2, 3]
    assert [turn_of(wayland_transform(turn)) for turn in TURNS] == list(TURNS)


@pytest.mark.parametrize("transform", [4, 5, 6, 7, -1, 8, 90, 1.0, True, False, "1", None])
def test_a_flipped_or_unknown_transform_is_no_turn(transform):
    with pytest.raises(ValueError, match="^display_turn$"):
        turn_of(transform)


@pytest.mark.parametrize("turn", [45, 360, -90, 1, 90.0, True, False, None])
def test_only_a_quarter_turn_has_a_transform(turn):
    with pytest.raises(ValueError, match="^display_turn$"):
        wayland_transform(turn)
    with pytest.raises(ValueError, match="^display_turn$"):
        hanging((3840, 2160), turn)


def test_a_screen_hung_sideways_swaps_its_sides_and_only_then():
    assert [hanging((3840, 2160), turn) for turn in TURNS] == [
        (3840, 2160), (2160, 3840), (3840, 2160), (2160, 3840)]


@pytest.mark.parametrize("size", [(0, 2160), (3840, 0), (True, 2160), (3840.0, 2160), (3840, -1)])
def test_a_screen_without_whole_pixels_has_no_size(size):
    with pytest.raises(ValueError, match="^screen_size$"):
        hanging(size, 0)
