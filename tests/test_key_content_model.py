"""Key content's validators and wire shape (roadmap 5a, run ledger .claude/runs/photo-5a.md,
slice R1)."""
from __future__ import annotations

import math

import pytest
from pydantic import TypeAdapter, ValidationError
from support.key_content_builders import banded_grid, face, uniform_grid

from central.wall_plan.key_content import (
    ANALYSIS_VERSION,
    Analysis,
    InterestGrid,
    KeyContent,
    Measured,
    MeasureFailed,
    UnitBox,
)


@pytest.mark.parametrize("box", [
    dict(left=.5, top=0, right=.5, bottom=1),
    dict(left=.6, top=0, right=.5, bottom=1),
    dict(left=0, top=.5, right=1, bottom=.5),
    dict(left=0, top=.7, right=1, bottom=.2),
])
def test_a_box_must_have_area(box) -> None:
    with pytest.raises(ValidationError):
        UnitBox(**box)


@pytest.mark.parametrize("weights", [
    (.5, .5, 0),                     # one weight short of 2 x 2
    (.25, .25, .25, .25, 0),         # one too many
    (.5, .5, .5, -.5),               # a negative weight
    (.5, .5, math.nan, 0),           # not finite
    (.5, .5, math.inf, 0),
    (.25, .25, .25, .2),             # sums to .95
])
def test_an_interest_grid_must_be_a_distribution_over_its_cells(weights) -> None:
    with pytest.raises(ValidationError):
        InterestGrid(columns=2, rows=2, weights=weights)


def test_the_builders_make_valid_grids() -> None:
    assert math.fsum(uniform_grid().weights) == pytest.approx(1)
    grid = banded_grid(top=.25, weight=.9)
    top_rows = grid.weights[:2 * grid.columns]     # rows wholly inside the band
    assert min(top_rows) > max(grid.weights[3 * grid.columns:])


@pytest.mark.parametrize("analysis", [
    Measured(key=KeyContent(analysis_version=ANALYSIS_VERSION, faces=(face(.1, .2, .3, .4),),
                            interest=banded_grid(left=.2))),
    MeasureFailed(reason="timeout"),
])
def test_an_analysis_round_trips_through_json(analysis) -> None:
    adapter = TypeAdapter(Analysis)
    assert adapter.validate_json(adapter.dump_json(analysis)) == analysis
