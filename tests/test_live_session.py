"""The Live model's shared session rules (roadmap 1c, slice C2; central/live_session.py): a lease an
edit or keepalive slides, never past its hard deadline, and the expected sequence every edit names.
Position's trial (central/fleet/node_calibration.py) and the Display's picture session both use them.
"""
from __future__ import annotations

import pytest

from central.live_session import Lease, StaleSequence, begin_lease, next_sequence, touched


def test_a_lease_slides_with_each_touch_but_never_past_its_hard_deadline():
    """Mutation probe: drop the min with hard_until -> the third touch passes 110."""
    lease = begin_lease(100.0, idle_seconds=5, hard_seconds=10)
    assert lease == Lease(105.0, 110.0)
    assert touched(lease, 103.0, idle_seconds=5) == Lease(108.0, 110.0)
    assert touched(touched(lease, 103.0, idle_seconds=5), 107.0, idle_seconds=5) == Lease(110.0, 110.0)
    unbounded = begin_lease(100.0, idle_seconds=5, hard_seconds=None)
    assert touched(unbounded, 104.0, idle_seconds=5) == Lease(109.0, None)


def test_an_ended_lease_is_refused_and_live_means_before_both_deadlines():
    lease = Lease(105.0, 110.0)
    assert lease.live(104.999) and not lease.live(105.0)
    assert not Lease(120.0, 110.0).live(110.0)
    assert Lease(105.0, None).live(104.0)
    with pytest.raises(ValueError, match="lease_ended"):
        touched(lease, 105.0, idle_seconds=5)


@pytest.mark.parametrize(("idle", "hard"), [(0, None), (-1, 10), (11, 10)])
def test_a_lease_needs_a_positive_idle_window_within_its_hard_one(idle, hard):
    with pytest.raises(ValueError, match="lease_bounds"):
        begin_lease(0.0, idle_seconds=idle, hard_seconds=hard)


def test_an_edit_names_the_sequence_it_expects():
    """Mutation probe: accept any expected sequence -> the stale edit is taken."""
    assert next_sequence(3, 3) == 4
    with pytest.raises(StaleSequence):
        next_sequence(3, 2)
    with pytest.raises(StaleSequence):
        next_sequence(3, 4)
