"""Focused checks for the public Ubuntu input verification boundary."""

import subprocess

import pytest

from scripts import fetch_ubuntu


def test_gpg_verification_disables_agent_and_keeps_bounded_diagnostics(tmp_path, monkeypatch, capsys):
    calls = []

    def failed_run(command, **kwargs):
        calls.append((command, kwargs))
        raise subprocess.CalledProcessError(2, command, stderr="discarded-prefix" + "x" * 8192 + "gpg: failed to start gpg-agent\n")

    monkeypatch.setattr(fetch_ubuntu.subprocess, "run", failed_run)
    with pytest.raises(ValueError, match="signature_tool_failed"):
        fetch_ubuntu._gpg(tmp_path, "--import", str(tmp_path / "ubuntu-cdimage.asc"))

    command, kwargs = calls[0]
    assert command[:5] == ["gpg", "--batch", "--no-autostart", "--homedir", str(tmp_path)]
    assert kwargs == {"check": True, "capture_output": True, "text": True, "timeout": 30}
    diagnostic = capsys.readouterr().err
    assert "failed to start gpg-agent" in diagnostic
    assert "discarded-prefix" not in diagnostic
    assert len(diagnostic) <= fetch_ubuntu.GPG_DIAGNOSTIC_LIMIT


def test_gpg_returns_status_output_for_signer_check(tmp_path, monkeypatch):
    status = "[GNUPG:] VALIDSIG " + fetch_ubuntu.SIGNER + " 20260905 0 4 0 1 10 abc\n"

    def successful_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=status, stderr="")

    monkeypatch.setattr(fetch_ubuntu.subprocess, "run", successful_run)
    result = fetch_ubuntu._gpg(tmp_path, "--status-fd", "1", "--verify", "signed", "checksums")

    assert result.stdout.startswith("[GNUPG:] VALIDSIG " + fetch_ubuntu.SIGNER + " ")
