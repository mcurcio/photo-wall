"""The kernel's verdict on the wall clock (appliance/kernel/clock.py `wall_clock_state`, decision
0032): the one judge of wall time on a Node, from adjtimex(2) alone. Central is never an input."""
import sys

import pytest

from appliance.kernel.clock import wall_clock_state


@pytest.mark.parametrize(("answer", "state"), [
    ((0, 0), "synchronised"),           # TIME_OK, STA_UNSYNC clear
    ((1, 0x2001), "synchronised"),      # TIME_INS with STA_PLL | STA_NANO: still disciplined
    ((5, 0x40), "unsynchronised"),      # TIME_ERROR and STA_UNSYNC
    ((0, 0x40), "unsynchronised"),      # STA_UNSYNC alone (mutation: ignore it -> synchronised)
    ((5, 0), "unsynchronised"),         # TIME_ERROR alone
])
def test_the_verdict_reads_time_error_and_sta_unsync(answer, state):
    assert wall_clock_state(lambda: answer) == state


@pytest.mark.parametrize("error", [OSError(1, "not permitted"), AttributeError("adjtimex")])
def test_a_clock_that_cannot_be_read_is_unknown_never_a_guess(error):
    def fails():
        raise error
    assert wall_clock_state(fails) is None


def test_the_default_reader_asks_this_kernel():
    """On Linux a read-only adjtimex answers (the struct's status read at its LP64 offset); off
    Linux libc has no adjtimex and the verdict is unknown."""
    expected = ("synchronised", "unsynchronised") if sys.platform.startswith("linux") else (None,)
    assert wall_clock_state() in expected
