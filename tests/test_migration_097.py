"""097 (calibration targets, decision 0026): a picture session's test pattern ask and a sent picture
entry may name either calibration target, and a sent entry still nothing outside contracts/tuning.py
TestPattern.

The session's ask goes through the operator API and the sent entry through the picture projection
(tests/test_picture_sent_history.py's helpers), so the two CHECKs are proved where they bite.
"""
from __future__ import annotations

import psycopg
import pytest
from test_display_identity import _sql
from test_display_picture_settings import _begin, _bound, _client, _picture
from test_picture_sent_history import _kinds, _pattern

from contracts.tuning import TestPattern


def test_097_a_session_asks_for_each_target_and_its_sent_entry_names_it(registry):
    """Mutation probe: drop 'target-post-software' from 122b's picture_history CHECK in a scratch
    copy -> the projection of the second ask raises CheckViolation and this fails."""
    _bound(registry)
    with _client(registry) as client:
        _picture(registry)
        session = _begin(client)
        _pattern(client, session, TestPattern.TARGET_PRE_SOFTWARE.value, 1)
        _picture(registry)
        _pattern(client, session, TestPattern.TARGET_POST_SOFTWARE.value, 2)
        _picture(registry)
    sid = session["session_id"]
    assert _kinds(registry) == [(1, "sent-kept", None, None), (2, "sent-draft", sid, "target-pre-software"),
                                (3, "sent-draft", sid, "target-post-software")]


def test_097_still_refuses_a_pattern_outside_the_vocabulary(registry):
    """A sent entry's pattern stays in the vocabulary (picture_history's CHECK since 122b; a session's
    ask is model-validated on the Display's row). Mutation probe: replace the CHECK by one that takes
    any text -> the update is taken."""
    _bound(registry)
    with _client(registry) as client:
        _picture(registry)
        _begin(client)
    with pytest.raises(psycopg.errors.CheckViolation):
        _sql(registry, "UPDATE picture_history SET test_pattern='target' WHERE kind='sent' AND picture_change=1")
