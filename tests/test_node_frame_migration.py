"""Historical 059 loss rows must survive the 060 identity upgrade as fences."""
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

MIGRATION = Path(__file__).parents[1] / 'central/migrations/060_node_output_frame_identity.sql'


def historical_tables(conn):
    # Real 059 table shape, with irrelevant foreign keys omitted to isolate DDL.
    conn.execute('CREATE TEMP TABLE node_output_losses (LIKE node_output_losses INCLUDING DEFAULTS)')
    conn.execute('ALTER TABLE node_output_losses DROP COLUMN frame_id')
    conn.execute('ALTER TABLE node_output_losses ADD PRIMARY KEY(player_id,authority_epoch,output_id,binding_generation)')
    conn.execute('CREATE TEMP TABLE execution_events (LIKE execution_events INCLUDING DEFAULTS)')


def loss(conn, output='out', resolved=None):
    producer, evidence = uuid4(), uuid4()
    detail = dict(cause='node_output_lost', producer_id=str(producer), evidence_id=str(evidence), evidence_kind='event')
    conn.execute('INSERT INTO node_output_losses(player_id,authority_epoch,output_id,binding_generation,'
        'configuration_revision,cause_producer_id,cause_evidence_id,cause_kind,detail,interrupted_at,resolved_at) '
        'VALUES(%s,1,%s,1,1,%s,%s,%s,%s,1000,%s)', ('player', output, producer, evidence, 'event', Jsonb(detail), resolved))
    return {**detail, 'output_id': output, 'authority_epoch': 1, 'binding_generation': 1}


def observation(conn, detail, frame, **changes):
    conn.execute('INSERT INTO execution_events(occurred_at,player_id,kind,detail) VALUES(1000.001,%s,%s,%s)',
        ('player', 'observation', Jsonb({**detail, 'frame_id': frame, **changes})))


def test_historical_unique_frame_backfill_deduplicates_exact_witness_and_keeps_resolved(registry):
    with registry.db.transaction() as conn:
        historical_tables(conn)
        original = loss(conn)
        observation(conn, original, 'original-frame')
        observation(conn, original, 'original-frame')
        loss(conn, 'resolved', 1001)
        conn.execute(MIGRATION.read_text())
        rows = conn.execute('SELECT output_id,frame_id,resolved_at FROM node_output_losses ORDER BY output_id').fetchall()
        assert rows == [dict(output_id='out', frame_id='original-frame', resolved_at=None),
                        dict(output_id='resolved', frame_id='', resolved_at=1001)]
        # Equal counters on a replacement Frame do not alias the retained fence.
        conn.execute("INSERT INTO node_output_losses SELECT player_id,authority_epoch,output_id,binding_generation,"
            "configuration_revision,cause_producer_id,cause_evidence_id,cause_kind,detail,interrupted_at,resolved_at,'new-frame' "
            "FROM node_output_losses WHERE output_id='out'")


@pytest.mark.parametrize('witness', ['missing', 'ambiguous', 'wrong_cause', 'wrong_epoch'])
def test_unresolved_historical_mapping_aborts_entire_migration(registry, witness):
    with registry.db.transaction() as conn:
        historical_tables(conn)
        good = loss(conn, 'good')
        observation(conn, good, 'known')
        bad = loss(conn, 'bad')
        loss(conn, 'resolved', 1001)
        if witness == 'ambiguous':
            observation(conn, bad, 'frame-a')
            observation(conn, bad, 'frame-b')
        elif witness == 'wrong_cause':
            observation(conn, bad, 'frame-a', evidence_id=str(uuid4()))
        elif witness == 'wrong_epoch':
            observation(conn, bad, 'frame-a', authority_epoch=2)
        with pytest.raises(psycopg.errors.RaiseException, match='node_output_loss_frame_unresolved'):
            with conn.transaction():
                conn.execute(MIGRATION.read_text())
        columns = conn.execute("SELECT attname FROM pg_attribute WHERE attrelid='node_output_losses'::regclass "
                               'AND attnum>0 AND NOT attisdropped').fetchall()
        assert 'frame_id' not in {c['attname'] for c in columns}
        assert conn.execute('SELECT count(*) AS n FROM node_output_losses').fetchone()['n'] == 3
        assert conn.execute("SELECT to_regclass('pg_temp.node_loss_frame_backfill') AS table_name").fetchone()['table_name'] is None
