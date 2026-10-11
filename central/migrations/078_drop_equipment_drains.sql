-- The equipment drain fence goes. Its only writer was the deleted V1 command path; every reader
-- (Runtime admission, Coordination, Registry's equipment writes, media grants, node Stage, the
-- rollout gate) admitted everything once nothing wrote a drain. A drain's cancel mode for
-- coordination groups goes with it: skipped groups are always revoked as `invalidate`.
DROP VIEW active_equipment_drains;
DROP TABLE equipment_drains;
ALTER TABLE coordination_groups DROP COLUMN drain_cancel;
