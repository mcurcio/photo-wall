"""The fixture can initiate a trial, but never control failed-trial recovery."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from scripts import vm_rollback_control as control

BOOT = '01234567-89ab-cdef-0123-456789abcdef'


def report():
    return dict(schema=2, boot_id=BOOT, device_id='device-'+'d'*64, ticket_id='e'*48,
                release_id='a'*64, trial=False, persistence='volatile', fault=None)


def record():
    current = {k: report()[k] for k in ('boot_id', 'device_id', 'release_id')}
    current['ticket_sha256'] = hashlib.sha256(report()['ticket_id'].encode()).hexdigest()
    return dict(schema=2, action='reboot-for-trial', current=current, candidate=dict(release_id='b'*64))


@pytest.mark.parametrize('fault', ['schema', 'action', 'command', 'ticket', 'candidate'])
def test_fixed_control_rejects_arbitrary_actions_and_unbound_context(tmp_path, fault):
    value = record()
    if fault == 'schema':
        value['schema'] = 1
    elif fault == 'action':
        value['action'] = 'shell'
    elif fault == 'command':
        value['command'] = 'anything'
    elif fault == 'ticket':
        value['current']['ticket_sha256'] = 'wrong'
    else:
        value['candidate']['release_id'] = value['current']['release_id']
    path = tmp_path / 'control.json'
    path.write_text(json.dumps(value))
    with pytest.raises(control.ControlError, match='invalid_control'):
        control._control(path)


@pytest.mark.parametrize('fault', [None, 'stale', 'mismatch', 'trial'])
def test_watcher_only_reboots_exact_requested_accepted_boot(tmp_path, monkeypatch, fault, capsys):
    value = record()
    if fault == 'stale':
        value['current']['boot_id'] = '11234567-89ab-cdef-0123-456789abcdef'
    if fault == 'mismatch':
        value['current']['ticket_sha256'] = 'f'*64
    path = tmp_path / 'control.json'
    path.write_text(json.dumps(value))
    monkeypatch.setattr(control, '_boot_id', lambda _: BOOT)
    def current(*_):
        if fault == 'trial':
            raise control.ControlError('boot_not_accepted')
        return report()
    monkeypatch.setattr(control, '_report', current)
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0)
    if fault == 'mismatch':
        with pytest.raises(control.ControlError, match='control_boot_mismatch'):
            control.run_watcher(control=path, command_runner=runner)
    else:
        assert control.run_watcher(control=path, command_runner=runner) == (fault is None)
    assert calls == ([['/usr/bin/systemctl', '--no-block', 'reboot']] if fault is None else [])
    assert report()['ticket_id'] not in capsys.readouterr().out
