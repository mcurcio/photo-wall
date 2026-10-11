-- The effect gate and its verifier chain go (owner answer G-A, "no effect gate"; 2026-10-10:
-- "remove dead code related to the v1 path. It's dead. Kill it."). Operator Reboot and Stage
-- are judged by their own owners alone (node_commands.py, node_lifecycle.py); nothing reads the
-- gate row, the rollout evidence heads or a command's gate stamp any more.
--
--   fleet_effect_gate and fleet_guard_effect_gate()   RolloutEffectGate (rollout_gate.py) and
--                                                     GET /v1/operator/node/status.
--   node_rollout_evidence_heads                       the Kubernetes verifier's replay heads.
--   node_reboot_commands.gate_generation,             the reboot issue and poll gate stamp.
--   node_reboot_commands.gate_scope_sha256
--   node_app_operations.rollout_generation,           the stage and desired-read gate stamp.
--   node_app_operations.rollout_scope_sha256

DROP TABLE fleet_effect_gate;
DROP FUNCTION fleet_guard_effect_gate();
DROP TABLE node_rollout_evidence_heads;
ALTER TABLE node_reboot_commands DROP COLUMN gate_generation, DROP COLUMN gate_scope_sha256;
ALTER TABLE node_app_operations DROP COLUMN rollout_generation, DROP COLUMN rollout_scope_sha256;
