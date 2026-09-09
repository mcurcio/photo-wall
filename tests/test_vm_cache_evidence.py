"""A new native presentation alone cannot prove post-reboot reacquisition."""
import pytest

from scripts.vm_cache_evidence import CacheEvidenceError, rehydration


def fixtures():
    before = dict(player_id='p', authority_epoch=1, sha256='a' * 64, size=123)
    after = before | dict(authority_epoch=2)
    boot = dict(device_id='device-'+'b'*64, boot_id='old', persistence='volatile')
    return before, after, boot, boot | dict(boot_id='new')


def test_rehydration_requires_new_boot_fresh_authority_and_same_photo():
    result = rehydration(*fixtures(), delivery_observed=True)
    assert result == dict(schema=2, sha256='a'*64, size=123, reboot_observed=True,
                          delivery_observed=True, native_presentation=True, cache_persistence='volatile')


@pytest.mark.parametrize('fault', ['delivery', 'boot', 'equipment', 'session', 'hash', 'size', 'persistence'])
def test_incomplete_rehydration_evidence_fails_closed(fault):
    before, after, boot, new_boot = fixtures()
    if fault == 'boot':
        new_boot['boot_id'] = boot['boot_id']
    if fault == 'equipment':
        new_boot['device_id'] = 'other'
    if fault == 'session':
        after['authority_epoch'] = 1
    if fault == 'hash':
        after['sha256'] = 'c'*64
    if fault == 'size':
        after['size'] = 124
    if fault == 'persistence':
        new_boot['persistence'] = 'durable'
    with pytest.raises(CacheEvidenceError, match='media_rehydration_unproven'):
        rehydration(before, after, boot, new_boot, delivery_observed=fault != 'delivery')
