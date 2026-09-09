ALTER TABLE players ADD COLUMN device_id TEXT NOT NULL;
ALTER TABLE players ADD CONSTRAINT players_device_id_unique UNIQUE(device_id);
