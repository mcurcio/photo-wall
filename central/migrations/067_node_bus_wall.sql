-- E3d: the highest WALL sequence Central recorded (nodeapi.hub.WallMarks); WALL is re-created at mark + 1 + K.
CREATE TABLE node_bus_wall (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    mark BIGINT NOT NULL CHECK (mark >= 0)
);
