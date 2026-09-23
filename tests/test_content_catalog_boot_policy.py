"""`choose_base` / `newest`: the pure port of `netboot_base._resolve_recovery_aware`.

Every scenario of `test_netboot_base_recovery.py` and `test_netboot_base_tracer.py` that
exercises selection is ported here with assertions on BOTH `tags` and `update`. Tags follow the
recovery suite: T1 = a device's known-good, T = a target that failed, T2 = a newer frontier.
"""

from __future__ import annotations

import pytest

from central.content_catalog.boot_policy import BootChoice, choose_base, newest
from central.content_catalog.ports import DeviceRow, DeviceUpdate

T1, T, T2 = "v0.0.1", "v0.0.2", "v0.0.3"
FENCE_T = DeviceUpdate(failed_tag=T, mark_boot_failed=True)
CLEAR = DeviceUpdate(failed_tag=None, mark_boot_failed=False)


def device(device_id: str, *, attached_tag=None, known_good_tag=None, last_served_tag=None,
           boot_outcome=None, failed_tag=None, last_served_at=None) -> DeviceRow:
    """A `DeviceRow` with every other field in the freshly-seen state."""
    return DeviceRow(device_id, None, attached_tag, known_good_tag, last_served_tag, boot_outcome,
                     failed_tag, last_served_at, False)


# -- newest -------------------------------------------------------------------------------------


def test_newest_ranks_by_semver_and_ignores_invalid_tags():
    assert newest([]) is None
    assert newest(["v0.10.0", "v0.9.0"]) == "v0.10.0"  # numeric, not lexical
    assert newest(["v1.0.0-rc.1", "v1.0.0"]) == "v1.0.0"  # a full release outranks its rc
    assert newest(["v1.0.0-rc.1", "v0.9.0"]) == "v1.0.0-rc.1"
    # Tracer probe: a non-semver tag (passes the PK CHECK) never wins over a valid lower one.
    assert newest(["v0.9.0", "v1.0.0.0"]) == "v0.9.0"
    assert newest(["garbage", "v1.2"]) is None


# -- no device row / no release -----------------------------------------------------------------


def test_absent_serial_serves_desired_and_writes_nothing():
    assert choose_base(None, frontier=T2, bootstrap=T) == BootChoice((T2,), False, None)
    assert choose_base(None, frontier=None, bootstrap=T) == BootChoice((T,), False, None)


def test_nothing_desired_and_no_pin_is_no_release():
    assert choose_base(None, frontier=None, bootstrap=None) is None
    # A known-good alone is not a target: legacy returned desired (None) -> nothing to serve.
    assert choose_base(device("d", known_good_tag=T1), frontier=None, bootstrap=None) is None


# -- tracer criteria ----------------------------------------------------------------------------


def test_tracer_empty_frontier_bootstraps_latest_discovered():
    # criterion 1: no device is known-good, so desired = the bootstrap tag.
    assert choose_base(device("d"), frontier=None, bootstrap=T) == BootChoice((T,), False, None)


def test_tracer_unpinned_device_follows_latest_verified_over_bootstrap():
    # criterion 4: a second, fresh device follows the frontier another device verified.
    assert choose_base(device("d"), frontier=T2, bootstrap=T) == BootChoice((T2,), False, None)


def test_healthy_device_on_desired_is_served_desired_without_a_write():
    # criterion 3 aftermath: known-good == desired, so there is no distinct substitute.
    row = device("d", known_good_tag=T, last_served_tag=T, boot_outcome="healthy")
    assert choose_base(row, frontier=T, bootstrap=None) == BootChoice((T,), False, None)


# -- pin ----------------------------------------------------------------------------------------


def test_pin_wins_over_the_frontier():
    row = device("d", attached_tag=T1, known_good_tag=T)
    assert choose_base(row, frontier=T2, bootstrap=None) == BootChoice((T1,), True, None)


def test_pin_wins_even_when_nothing_else_is_desired():
    row = device("d", attached_tag=T1)
    assert choose_base(row, frontier=None, bootstrap=None) == BootChoice((T1,), True, None)


def test_pin_clears_a_live_fence():
    row = device("d", attached_tag=T1, failed_tag=T, known_good_tag=T1)
    assert choose_base(row, frontier=T, bootstrap=None) == BootChoice((T1,), True, CLEAR)


def test_pin_on_a_pending_desired_does_not_detect():
    # The pin short-circuits before DETECT, exactly as legacy.
    row = device("d", attached_tag=T, last_served_tag=T, boot_outcome="pending")
    assert choose_base(row, frontier=T, bootstrap=None) == BootChoice((T,), True, None)


# -- recovery (a)-(e) ---------------------------------------------------------------------------


def test_a_re_netboot_while_pending_detects_a_failed_boot():
    # (a) 200-served T, back asking while pending, no known-good: fence T, keep serving T.
    row = device("d", last_served_tag=T, boot_outcome="pending", last_served_at=1000.0)
    assert choose_base(row, frontier=T, bootstrap=None) == BootChoice((T,), False, FENCE_T)


def test_b_recovery_serves_known_good_and_keeps_the_stick():
    # (b) DETECT fences T, then RECOVER serves T1; the fence is the only write.
    row = device("d", known_good_tag=T1, last_served_tag=T, boot_outcome="pending",
                 last_served_at=1000.0)
    assert choose_base(row, frontier=T, bootstrap=None) == BootChoice((T1,), False, FENCE_T)


def test_c_after_the_recovery_boot_confirms_healthy_the_stick_holds():
    # (c) The recovery boot reported healthy on T1; failed_tag stays T, so the next boot
    # still recovers to T1 and writes nothing.
    row = device("d", known_good_tag=T1, last_served_tag=T1, boot_outcome="healthy",
                 failed_tag=T)
    assert choose_base(row, frontier=T, bootstrap=None) == BootChoice((T1,), False, None)


def test_d_repeated_reboots_keep_serving_known_good_no_oscillation():
    # (d) last_served == T1 != desired, so DETECT stays quiet and RECOVER holds every time.
    row = device("d", known_good_tag=T1, last_served_tag=T1, boot_outcome="pending",
                 failed_tag=T, last_served_at=1000.0)
    for _ in range(3):
        assert choose_base(row, frontier=T, bootstrap=None) == BootChoice((T1,), False, None)


def test_e_newer_latest_verified_releases_the_stick():
    # (e) desired moved to T2 past the fence T: clear the fence, one fresh attempt on T2,
    # with the known-good T1 offered as the substitute.
    row = device("d", known_good_tag=T1, last_served_tag=T1, boot_outcome="pending",
                 failed_tag=T, last_served_at=1000.0)
    assert choose_base(row, frontier=T2, bootstrap=None) == BootChoice((T2, T1), False, CLEAR)


def test_release_without_a_known_good_serves_desired_alone():
    row = device("d", failed_tag=T, boot_outcome="failed", last_served_tag=T)
    assert choose_base(row, frontier=T2, bootstrap=None) == BootChoice((T2,), False, CLEAR)


def test_fenced_with_no_known_good_boot_loops_on_desired():
    # Unchanged legacy behaviour: no rollback target, so desired is served until a pin.
    row = device("d", failed_tag=T, boot_outcome="failed", last_served_tag=T)
    assert choose_base(row, frontier=T, bootstrap=None) == BootChoice((T,), False, None)


def test_detect_after_a_sweep_fence_keeps_the_fence_on_desired():
    # The sweep already failed + fenced T; a failed (not pending) outcome never re-detects.
    row = device("d", known_good_tag=T1, failed_tag=T, boot_outcome="failed",
                 last_served_tag=T)
    assert choose_base(row, frontier=T, bootstrap=None) == BootChoice((T1,), False, None)


# -- substitution (design §10.4) ----------------------------------------------------------------


def test_unpinned_offers_known_good_as_a_substitute():
    row = device("d", known_good_tag=T1, last_served_tag=T1, boot_outcome="healthy")
    assert choose_base(row, frontier=T2, bootstrap=None) == BootChoice((T2, T1), False, None)


def test_a_substitute_boot_does_not_trip_detect():
    # The substitute T1 was recorded as served; desired T2 != last_served, so no fence.
    row = device("d", known_good_tag=T1, last_served_tag=T1, boot_outcome="pending",
                 last_served_at=1000.0)
    assert choose_base(row, frontier=T2, bootstrap=None) == BootChoice((T2, T1), False, None)


def test_boot_choice_invariants():
    with pytest.raises(ValueError):
        BootChoice((), False, None)
    with pytest.raises(ValueError):
        BootChoice((T, T), False, None)
    with pytest.raises(ValueError):
        BootChoice((T, T1), True, None)


# -- owner ruling: unpinned may get the newest ready eligible version; pinned never substitutes --

V1, V2, V3, V4 = "v0.1.0", "v0.2.0", "v0.3.0", "v0.4.0"
BOOTABLE = (V1, V2, V3, V4)  # full releases with an OS image (the catalog passes these)


def test_unpinned_offers_every_older_bootable_release_newest_first_not_only_its_known_good():
    row = device("d", known_good_tag=V1)
    assert choose_base(row, frontier=V3, bootstrap=None, substitutes=BOOTABLE) == BootChoice(
        (V3, V2, V1), False, None)  # V4 is newer than desired: never validated, not eligible


def test_unpinned_substitutes_never_include_the_fenced_tag():
    row = device("d", known_good_tag=V1, failed_tag=V2)  # the fence names a non-desired tag
    assert choose_base(row, frontier=V3, bootstrap=None, substitutes=BOOTABLE) == BootChoice(
        (V3, V1), False, CLEAR)


def test_absent_serial_may_substitute_too():
    assert choose_base(None, frontier=None, bootstrap=V2, substitutes=BOOTABLE) == BootChoice(
        (V2, V1), False, None)


def test_pinned_never_gets_a_substitute():
    row = device("d", attached_tag=V3, known_good_tag=V1)
    assert choose_base(row, frontier=V4, bootstrap=None, substitutes=BOOTABLE) == BootChoice(
        (V3,), True, None)


def test_recovery_still_serves_only_the_known_good():
    row = device("d", known_good_tag=V1, failed_tag=V3, boot_outcome="failed")
    assert choose_base(row, frontier=V3, bootstrap=None, substitutes=BOOTABLE) == BootChoice(
        (V1,), False, None)
