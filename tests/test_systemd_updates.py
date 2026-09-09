"""Stateless watchdog unit wiring. Physical/systemd reboot qualification is separate."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_trial_watchdog_and_recovery_have_only_volatile_update_state():
    watch = (ROOT / "appliance/systemd/accept-trial.service").read_text()
    recovery = (ROOT / "appliance/systemd/trial-recovery.service").read_text()
    assert "appliance.updates watch-current" in watch
    assert "OnFailure=photo-wall-trial-recovery.service" in watch
    assert "appliance.updates reboot-required" in recovery
    assert "ExecStart=/usr/bin/systemctl --no-block reboot" in recovery
    for unit in (watch, recovery):
        assert "/var/lib/photo-wall" not in unit
        assert "ReadWritePaths=/run/photo-wall" in unit


def test_systemd_takes_over_the_watchdog_armed_by_trusted_bootstrap():
    manager = (ROOT / "appliance/systemd/watchdog.conf").read_text()
    assert "RuntimeWatchdogSec=30s" in manager
    assert "RebootWatchdogSec=5min" in manager
