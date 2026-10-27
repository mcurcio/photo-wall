-- Central marks the light it can't stand behind (the Frame shows Central's error when it blacks the
-- Frame, 2026-10-27): a reference's "since when it can't tell" becomes its doubt, why Central can't
-- stand behind its last word and since when (central/room_light/model.py Doubt): "cant-tell" (its
-- source said it cannot tell), "cant-read" (Central refused its report as unreadable) or
-- "link-closed" (its source's connection to Central's hub closed). The reasons and when each is set
-- or cleared are app code (central/room_light/policy.py, central/infra/room_light_store.py); no
-- trigger, no stored procedure.
--
-- Upgrade data: every kept "can't tell since" becomes a "cant-tell" doubt since then. Galleria's
-- stored birth is removed: a birth now names the hub connection it was put on, so one stored by an
-- older Galleria no longer reads; Galleria puts its birth again on every connection. No backwards
-- compatibility in the data.
--
-- light_words and reading_words gain the audit fields colour_words has (109): when each row was
-- created and by whom, when last changed and by whom, written by central/infra/room_light_store.py.
-- Their rows now also change by Central's own judgement (a refused report, a closed link), so "who"
-- is the reference's source for what it stated and "central" for a doubt Central set. Upgrade data:
-- each existing row reads as created and last changed by its source when it was last heard.

ALTER TABLE light_words RENAME COLUMN cant_tell_since TO doubt_since;
ALTER TABLE light_words ADD COLUMN doubt_reason TEXT NULL;
UPDATE light_words SET doubt_reason = 'cant-tell' WHERE doubt_since IS NOT NULL;
ALTER TABLE light_words ADD CONSTRAINT light_words_doubt_whole CHECK ((doubt_reason IS NULL) = (doubt_since IS NULL));
ALTER TABLE light_words ADD COLUMN created_at TIMESTAMPTZ NULL, ADD COLUMN created_by TEXT NULL,
    ADD COLUMN changed_at TIMESTAMPTZ NULL, ADD COLUMN changed_by TEXT NULL;
UPDATE light_words SET created_at = heard_at, created_by = source, changed_at = heard_at, changed_by = source;
ALTER TABLE light_words ALTER COLUMN created_at SET NOT NULL, ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_at SET NOT NULL, ALTER COLUMN changed_by SET NOT NULL;

ALTER TABLE reading_words RENAME COLUMN cant_tell_since TO doubt_since;
ALTER TABLE reading_words ADD COLUMN doubt_reason TEXT NULL;
UPDATE reading_words SET doubt_reason = 'cant-tell' WHERE doubt_since IS NOT NULL;
ALTER TABLE reading_words ADD CONSTRAINT reading_words_doubt_whole CHECK ((doubt_reason IS NULL) = (doubt_since IS NULL));
ALTER TABLE reading_words ADD COLUMN created_at TIMESTAMPTZ NULL, ADD COLUMN created_by TEXT NULL,
    ADD COLUMN changed_at TIMESTAMPTZ NULL, ADD COLUMN changed_by TEXT NULL;
UPDATE reading_words SET created_at = heard_at, created_by = source, changed_at = heard_at, changed_by = source;
ALTER TABLE reading_words ALTER COLUMN created_at SET NOT NULL, ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_at SET NOT NULL, ALTER COLUMN changed_by SET NOT NULL;

ALTER TABLE colour_words RENAME COLUMN cant_tell_since TO doubt_since;
ALTER TABLE colour_words ADD COLUMN doubt_reason TEXT NULL;
UPDATE colour_words SET doubt_reason = 'cant-tell' WHERE doubt_since IS NOT NULL;
ALTER TABLE colour_words ADD CONSTRAINT colour_words_doubt_whole CHECK ((doubt_reason IS NULL) = (doubt_since IS NULL));

DELETE FROM galleria_birth;
