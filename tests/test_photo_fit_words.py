"""The console names every reason the photo fit rule gives (follow-up to 5a, slice S1): the
reasons in central/console/src/domain/photo-fit.ts (its `FitRefusal` type and `REASON_WORDS`, the
words' one home in the console) are exactly central/wall_plan/fit.py's `FitRefusal`, and the
fit analysis counts each shape reason the rule gives. A reason the rule gains and the console
lacks would draw a Fit board line with no words."""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from central.wall_plan.fit import FitRefusal
from central.wall_plan.fit_analysis import _CONTENT_REFUSALS, _SHAPE_REFUSALS

SOURCE = Path(__file__).resolve().parents[1] / "central/console/src/domain/photo-fit.ts"


def _console() -> tuple[set[str], dict[str, str]]:
    text = SOURCE.read_text()
    union = re.search(r"export type FitRefusal =([^;]+);", text)
    words = re.search(r"export const REASON_WORDS[^{]*\{([^}]+)\}", text)
    assert union and words
    reasons = set(re.findall(r'"([a-z_]+)"', union.group(1)))
    named = dict(re.findall(r'([a-z_]+):\s*"([^"]+)"', words.group(1)))
    return reasons, named


def test_every_reason_of_the_fit_rule_has_the_consoles_words():
    """Mutation probe 5 (drop a reason word): this fails."""
    reasons, named = _console()
    rule = set(get_args(FitRefusal))
    assert reasons == rule
    assert set(named) == rule
    assert all(words.strip() for words in named.values())
    assert named["above_player_limit"] == "needs more detail than this Pi can draw"
    assert set(_SHAPE_REFUSALS) | set(_CONTENT_REFUSALS) == rule
