-- Decision 0034, S2c. What Central reads from each Pi's bus is kept in three tables in place of
-- seven: one row per Pi (`node_links`: its cursors, Central's own document tokens, presence), the
-- newest value per state key (`node_states`), and one append-only log kept a week (`events`).
-- Written only by central/infra/node_link_store.py, central/infra/events.py and
-- central/fleet/node_bus_presence.py. Times are epoch seconds on the database's clock (presence's
-- on the worker's, as node_bus_presence.changed_at was); nothing here is a Pi's time. Plain
-- statements, no trigger or function, forward-only.
CREATE TABLE node_links (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id),
    cursors JSONB NOT NULL DEFAULT '{}'::jsonb,     -- {stream: {"pipe": "fleet"|"show", "epoch": text, "seq": int}}
    documents JSONB NOT NULL DEFAULT '{}'::jsonb,   -- {stream: {key: {"digest": text, "epoch": text, "seq": int}}}
    linked BOOLEAN,                                 -- whether the hub holds its leaf; NULL never looked
    linked_changed_at DOUBLE PRECISION,             -- the worker's clock, as node_bus_presence.changed_at
    created_at DOUBLE PRECISION NOT NULL, created_by TEXT NOT NULL,
    changed_at DOUBLE PRECISION NOT NULL, changed_by TEXT NOT NULL,
    CHECK (jsonb_typeof(cursors) = 'object' AND jsonb_typeof(documents) = 'object'),
    CHECK ((linked IS NULL) = (linked_changed_at IS NULL)));

CREATE TABLE node_states (
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    stream TEXT NOT NULL,                            -- 'KV_state_<line>'
    subject TEXT NOT NULL,                           -- the state key's subject
    epoch TEXT NOT NULL,
    seq BIGINT NOT NULL CHECK (seq > 0),
    removed BOOLEAN NOT NULL,                        -- a KV delete or purge marker
    headers JSONB NOT NULL,
    data BYTEA NOT NULL,
    recorded_at DOUBLE PRECISION NOT NULL,           -- when Central recorded this value
    PRIMARY KEY (device_id, stream, subject));

CREATE TABLE events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('record', 'gap', 'link-action', 'refusal')),
    device_id TEXT REFERENCES devices(device_id) ON DELETE CASCADE,   -- NULL for a refusal not from a Pi
    stream TEXT, epoch TEXT, seq BIGINT,             -- a record's key; a gap's stream, epoch, after_seq
    subject TEXT, message_id TEXT, headers JSONB, data BYTEA,          -- a record
    body JSONB,                                       -- a gap's {"lost"}, an action's body and kind, a refusal's reason
    recorded_at DOUBLE PRECISION NOT NULL,
    recorded_by TEXT NOT NULL);                       -- an Actor: "node" for a Pi's record, "central" for an action
CREATE UNIQUE INDEX events_record_key ON events (device_id, stream, epoch, seq) WHERE kind = 'record';
CREATE UNIQUE INDEX events_gap_key ON events (device_id, stream, epoch, seq) WHERE kind = 'gap';
CREATE INDEX events_kind_recorded ON events (kind, recorded_at);
CREATE INDEX events_recorded ON events (recorded_at);

-- One node_links row per device with any cursor, document or presence row; its writer before this
-- release is unknown.
INSERT INTO node_links (device_id, cursors, documents, linked, linked_changed_at, created_at,
                        created_by, changed_at, changed_by)
SELECT d.device_id,
       coalesce((SELECT jsonb_object_agg(c.stream, jsonb_build_object('pipe', c.pipe, 'epoch', c.epoch,
                                                                      'seq', c.seq))
                 FROM node_link_cursors c WHERE c.device_id = d.device_id), '{}'::jsonb),
       coalesce((SELECT jsonb_object_agg(s.stream, s.keys)
                 FROM (SELECT o.stream, jsonb_object_agg(o.key, jsonb_build_object(
                                  'digest', o.digest, 'epoch', o.epoch, 'seq', o.seq)) AS keys
                       FROM node_link_documents o WHERE o.device_id = d.device_id
                       GROUP BY o.stream) s), '{}'::jsonb),
       p.linked, p.changed_at,
       EXTRACT(EPOCH FROM clock_timestamp()), 'unknown', EXTRACT(EPOCH FROM clock_timestamp()), 'unknown'
FROM (SELECT device_id FROM node_link_cursors UNION SELECT device_id FROM node_link_documents
      UNION SELECT device_id FROM node_bus_presence) d
LEFT JOIN node_bus_presence p ON p.device_id = d.device_id;

-- The newest value of every state key (a KV stream), by Central's record time, then sequence, as
-- node_link_latest.py read it.
INSERT INTO node_states (device_id, stream, subject, epoch, seq, removed, headers, data, recorded_at)
SELECT DISTINCT ON (device_id, stream, subject)
       device_id, stream, subject, epoch, seq, headers ? 'KV-Operation', headers, data, recorded_at
FROM node_link_records
WHERE stream LIKE 'KV\_%'
ORDER BY device_id, stream, subject, recorded_at DESC, seq DESC;

INSERT INTO events (kind, device_id, stream, epoch, seq, subject, message_id, headers, data,
                    recorded_at, recorded_by)
SELECT 'record', device_id, stream, epoch, seq, subject, message_id, headers, data, recorded_at, 'node'
FROM node_link_records
WHERE stream NOT LIKE 'KV\_%'
ORDER BY recorded_at, device_id, stream, epoch, seq;

INSERT INTO events (kind, device_id, stream, epoch, seq, body, recorded_at, recorded_by)
SELECT 'gap', device_id, stream, epoch, after_seq, jsonb_build_object('lost', lost), recorded_at, 'central'
FROM node_link_gaps
ORDER BY recorded_at, device_id, stream, epoch, after_seq;

INSERT INTO events (kind, device_id, body, recorded_at, recorded_by)
SELECT 'link-action', device_id, jsonb_build_object('pipe', pipe, 'kind', kind, 'body', body),
       recorded_at, 'central'
FROM node_link_actions
ORDER BY action_id;

INSERT INTO singletons (name, document, revision, created_at, created_by, changed_at, changed_by)
SELECT 'wall_mark', jsonb_build_object('mark', mark), 1, EXTRACT(EPOCH FROM clock_timestamp()),
       'unknown', EXTRACT(EPOCH FROM clock_timestamp()), 'unknown'
FROM node_bus_wall;

DROP TABLE node_link_records, node_link_gaps, node_link_cursors, node_link_documents,
           node_link_actions, node_bus_presence, node_bus_wall;
