from dataclasses import replace
from uuid import uuid4

import pytest
from test_fleet_attempts import BOOT_ID, DEVICE_ID, SERIAL
from test_node_boot import claim_for, cold_setup

from central.fleet.node_observations import NodeObservations
from central.fleet.node_sessions import NodeControlError
from contracts.node_boot import NodeBootRequestV2
from contracts.node_preparation import (
    ManagerPreparationV2,
    encode_manager_preparation,
    parse_manager_preparation,
)


def test_manager_sample_retries_keep_original_age_and_never_authorize(registry):
    boots,sessions,_=cold_setup(registry)
    offer=boots.offer(NodeBootRequestV2(SERIAL,BOOT_ID,'c'*64))
    claim=claim_for(offer,owner='app_manager')
    grant=sessions.enroll(claim)
    sample=ManagerPreparationV2(grant.producer,1,1000,'verified',uuid4(),'d'*64,None,500,100)
    raw=encode_manager_preparation(sample)
    assert parse_manager_preparation(raw)==sample
    observations=NodeObservations(sessions)
    first=observations.record_preparation(claim.session_id,claim.credential,raw)
    registry.clock.advance(10)
    duplicate=observations.record_preparation(claim.session_id,claim.credential,raw)
    assert duplicate['received_at']==first['received_at'] and duplicate['authority_granted'] is False
    projected=observations.status(DEVICE_ID)['sessions'][0]['manager_preparation']
    assert projected['sample']['sampled_boottime_ms']==1000 and projected['receipt_age_seconds']==10
    with pytest.raises(NodeControlError,match='identity_conflict'):
        observations.record_preparation(claim.session_id,claim.credential,
            encode_manager_preparation(replace(sample,sampled_boottime_ms=11000)))
    with registry.db.transaction() as conn:
        assert conn.execute('SELECT count(*) n FROM node_app_operations').fetchone()['n']==0
