"""Installation read contracts accept partial feedback, not malformed inventory."""
import json

import pytest
from pydantic import ValidationError

from central.installation_models import (
    EnrollmentObservation,
    EquipmentSessionObservation,
    InstallationInventory,
)
from scripts.vm_inventory_probe import MAX_INVENTORY_BYTES, SESSIONS, enrollment_sessions

PLAYER = 'p-' + 'a' * 32
DEVICE = 'device-' + 'b' * 64


def inventory(**player_changes):
    return dict(players=[dict(id=PLAYER, device_id=DEVICE, authority_epoch=1,
                             registered_at=1000, last_seen=1000, retired_at=None, health={}) | player_changes],
                outputs=[], frames=[])


def test_registered_equipment_is_observable_before_health_or_output_feedback():
    expected = EquipmentSessionObservation(player_id=PLAYER, device_id=DEVICE, authority_epoch=1, retired=False)
    assert enrollment_sessions(json.dumps(inventory()).encode()) == (expected,)
    assert InstallationInventory.model_validate(inventory()).players[0].health == {}
    assert enrollment_sessions(b'{"players":[],"outputs":[],"frames":[]}') == ()


def test_enrollment_projection_omits_all_health_contents_and_credentials():
    raw = json.dumps(inventory(health={'ticket_id': 'private-ticket', 'nested': {'token': 'private-token'}})).encode()
    result = SESSIONS.dump_json(enrollment_sessions(raw))
    assert json.loads(result) == [dict(player_id=PLAYER, device_id=DEVICE, authority_epoch=1, retired=False)]
    assert b'private' not in result


@pytest.mark.parametrize('value', [{}, {'players': [], 'outputs': []}, {'players': None, 'outputs': [], 'frames': []}])
def test_missing_or_malformed_inventory_is_a_contract_error(value):
    with pytest.raises(ValidationError):
        enrollment_sessions(json.dumps(value).encode())


def test_inventory_is_bounded_before_json_validation():
    with pytest.raises(ValueError, match='inventory_limit'):
        enrollment_sessions(b' ' * (MAX_INVENTORY_BYTES + 1))


@pytest.mark.parametrize('epoch', [True, '1', 0])
def test_inventory_epoch_is_a_positive_integer(epoch):
    with pytest.raises(ValidationError):
        enrollment_sessions(json.dumps(inventory(authority_epoch=epoch)).encode())


def test_enrollment_observation_cannot_represent_ambiguous_readiness():
    session = EquipmentSessionObservation(player_id=PLAYER, device_id=DEVICE, authority_epoch=1, retired=False)
    with pytest.raises(ValidationError):
        EnrollmentObservation(state='ready')
    with pytest.raises(ValidationError):
        EnrollmentObservation(state='pending', session=session)
    assert EnrollmentObservation(state='ready', session=session).session == session


@pytest.mark.parametrize('collection', ['outputs', 'frames'])
def test_enrollment_projection_validates_the_complete_installation_contract(collection):
    value = inventory()
    value[collection] = [{'id': 'incomplete-nested-record'}]
    with pytest.raises(ValidationError):
        enrollment_sessions(json.dumps(value).encode())
