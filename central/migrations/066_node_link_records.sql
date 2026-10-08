-- E3d: what Central read from each Node's bus (E3b design §7.4, §9.3; nodeapi.hub.LinkStore). A message is
-- keyed by (Node, stream, epoch, sequence), so a repeat is a no-op. Times are Central's own clock only.
CREATE TABLE node_link_records (
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    stream TEXT NOT NULL,
    epoch TEXT NOT NULL,
    seq BIGINT NOT NULL CHECK (seq > 0),
    subject TEXT NOT NULL,
    message_id TEXT,                         -- the envelope's message id (events); NULL on state
    headers JSONB NOT NULL,                  -- every header as read (schema major, writer)
    data BYTEA NOT NULL,
    recorded_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (device_id, stream, epoch, seq)
);
CREATE TABLE node_link_gaps (
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    stream TEXT NOT NULL,
    epoch TEXT NOT NULL,
    after_seq BIGINT NOT NULL CHECK (after_seq >= 0),   -- the last sequence read before the gap
    lost BIGINT CHECK (lost > 0),                       -- NULL: unknown, the epoch ended
    recorded_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (device_id, stream, epoch, after_seq)
);
CREATE TABLE node_link_cursors (
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    stream TEXT NOT NULL,
    pipe TEXT NOT NULL CHECK (pipe IN ('fleet', 'show')),
    epoch TEXT NOT NULL,
    seq BIGINT NOT NULL CHECK (seq >= 0),
    PRIMARY KEY (device_id, stream)
);
CREATE TABLE node_link_documents (           -- Central's own last write per desired key (own tokens)
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    stream TEXT NOT NULL,
    key TEXT NOT NULL,
    digest TEXT NOT NULL,
    epoch TEXT NOT NULL,
    seq BIGINT NOT NULL CHECK (seq > 0),
    PRIMARY KEY (device_id, stream, key)
);
CREATE TABLE node_link_actions (             -- the action log: calls, discovery, refused and adopted documents
    action_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    pipe TEXT NOT NULL CHECK (pipe IN ('fleet', 'show')),
    kind TEXT NOT NULL,
    body JSONB NOT NULL,
    recorded_at DOUBLE PRECISION NOT NULL
);
