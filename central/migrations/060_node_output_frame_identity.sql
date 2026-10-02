-- A historical unresolved loss is still a fence. Recover its Frame only from
-- its original exact cause observation; current Registry bindings are unrelated.
ALTER TABLE node_output_losses ADD COLUMN frame_id TEXT NOT NULL DEFAULT '';
CREATE TEMP TABLE node_loss_frame_backfill ON COMMIT DROP AS
SELECT l.player_id,l.authority_epoch,l.output_id,l.binding_generation,
       MIN(e.detail->>'frame_id') AS frame_id,
       COUNT(DISTINCT e.detail->>'frame_id') AS frame_count
FROM node_output_losses l
JOIN execution_events e ON e.player_id=l.player_id AND e.kind='observation'
 AND e.occurred_at>=l.interrupted_at
 AND e.detail->>'cause'='node_output_lost'
 AND e.detail->>'producer_id'=l.cause_producer_id::text
 AND e.detail->>'evidence_id'=l.cause_evidence_id::text
 AND e.detail->>'evidence_kind'=l.cause_kind
 AND e.detail->>'authority_epoch'=l.authority_epoch::text
 AND e.detail->>'output_id'=l.output_id
 AND e.detail->>'binding_generation'=l.binding_generation::text
 AND e.detail @> l.detail
 AND jsonb_typeof(e.detail->'frame_id')='string'
 AND e.detail->>'frame_id'<>''
GROUP BY l.player_id,l.authority_epoch,l.output_id,l.binding_generation;
UPDATE node_output_losses l SET frame_id=b.frame_id
FROM node_loss_frame_backfill b
WHERE b.frame_count=1 AND l.player_id=b.player_id
 AND l.authority_epoch=b.authority_epoch AND l.output_id=b.output_id
 AND l.binding_generation=b.binding_generation;
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM node_output_losses WHERE resolved_at IS NULL AND frame_id='') THEN
  RAISE EXCEPTION 'node_output_loss_frame_unresolved: preserve historical fence; exact unique observation required';
 END IF;
END $$;
-- Resolved history may retain the empty sentinel; it cannot admit or fence a Frame.
ALTER TABLE node_output_losses ALTER COLUMN frame_id DROP DEFAULT;
ALTER TABLE node_output_losses DROP CONSTRAINT node_output_losses_pkey;
ALTER TABLE node_output_losses ADD PRIMARY KEY
    (player_id, authority_epoch, output_id, frame_id, binding_generation);
