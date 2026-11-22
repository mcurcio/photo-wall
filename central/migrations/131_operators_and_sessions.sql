-- Decision 0035: operator sign-in through an identity provider. Two tables, each one aggregate
-- with one repository (central/infra/operator_session_store.py; decision 0034), each row with its
-- audit fields. Nothing here in token mode: both stay empty until the deployment sets an issuer.
--
-- * `operators`: one row per provider identity (issuer, subject). `operator_id` is Central's own
--   id, the one an audit value names (`person:<operator_id>`, `client:<operator_id>`,
--   central/actor.py); never the provider's subject, a name or an e-mail. Written at first sight
--   and when the stated name changes.
-- * `operator_sessions`: one row per signed-in browser, keyed by the SHA-256 of its cookie's value
--   (the value itself is never kept). `refresh_token` and `id_token` are secrets at rest, as the
--   Immich key is (decision 0020): only the repository reads them. Written at sign-in and by each
--   re-judgement that changes something; deleted at Log out, when the provider ends it, and past
--   `hard_end` by the retention step (`hard_end`'s index is its walk).
-- Plain statements, no trigger or function, forward-only.
CREATE TABLE operators (
    operator_id TEXT PRIMARY KEY,
    issuer      TEXT NOT NULL,
    subject     TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('person', 'client')),
    name        TEXT NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL,
    created_by  TEXT NOT NULL,
    changed_at  DOUBLE PRECISION NOT NULL,
    changed_by  TEXT NOT NULL,
    UNIQUE (issuer, subject)
);

CREATE TABLE operator_sessions (
    session_sha256   TEXT PRIMARY KEY,
    operator_id      TEXT NOT NULL REFERENCES operators(operator_id),
    origin           TEXT NOT NULL,
    provider_session TEXT NULL,
    roles            TEXT[] NOT NULL,
    fresh_until      DOUBLE PRECISION NOT NULL,
    refresh_token    TEXT NULL,
    id_token         TEXT NULL,
    hard_end         DOUBLE PRECISION NOT NULL,
    created_at       DOUBLE PRECISION NOT NULL,
    created_by       TEXT NOT NULL,
    changed_at       DOUBLE PRECISION NOT NULL,
    changed_by       TEXT NOT NULL
);
CREATE INDEX operator_sessions_hard_end ON operator_sessions (hard_end);
CREATE INDEX operator_sessions_operator ON operator_sessions (operator_id);
CREATE INDEX operator_sessions_provider_session ON operator_sessions (provider_session)
    WHERE provider_session IS NOT NULL;
