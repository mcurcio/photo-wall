-- E3d: Central's hub holds a Node's leaf or not, as Central's last look saw it (0017 Open item; never gates).
CREATE TABLE node_bus_presence (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id),
    linked BOOLEAN NOT NULL,
    changed_at DOUBLE PRECISION NOT NULL      -- Central's clock at the look that saw the change
);
CREATE TABLE node_bus_hub_looks (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    looked_at DOUBLE PRECISION NOT NULL       -- Central's clock at the last look that reached the hub
);
