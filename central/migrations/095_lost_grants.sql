-- Lost grants (fleet lock and delivery churn, 2026-10-13): every Readiness report now says which
-- start grants the Player saw and no longer holds (contracts/models.py Readiness.lost_grants,
-- required). A stored report is read back as a Readiness (central/coordination.py _commit_due, the
-- readiness diagnostics, acceptance), so each one written before this field said nothing was lost.
-- The rule lives in app code; there is no trigger and no stored procedure here.
UPDATE player_feedback SET readiness = readiness || '{"lost_grants": []}'::jsonb
WHERE NOT readiness ? 'lost_grants';
