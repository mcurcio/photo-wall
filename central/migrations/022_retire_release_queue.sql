-- Central MVP P2.1: the legacy release queue has no consumer any more. Release sync and the
-- OS-image/.deb fetches are job types run by JobRuntime (design §10.5), so pending tasks left on
-- `photo-wall-app-release` would sit in `todo` forever. Cancel them. procrastinate's status
-- trigger records todo -> cancelled as a `cancelled` event. The guard makes this a no-op on a
-- fresh install, where Central applies procrastinate's schema only AFTER the migrations.
-- Forward-only (central/db.py); nothing to roll back, since the rows have no consumer.
DO $$
BEGIN
    IF to_regclass('procrastinate_jobs') IS NOT NULL THEN
        UPDATE procrastinate_jobs SET status = 'cancelled'
        WHERE queue_name = 'photo-wall-app-release' AND status = 'todo';
    END IF;
END
$$;
