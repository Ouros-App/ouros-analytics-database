DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'analytics_sync_rw'
    ) THEN
        CREATE ROLE analytics_sync_rw
            LOGIN
            NOINHERIT
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION
            NOBYPASSRLS
            CONNECTION LIMIT 2;
    END IF;
END
$$;

ALTER ROLE analytics_sync_rw
    LOGIN
    NOINHERIT
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOREPLICATION
    NOBYPASSRLS
    CONNECTION LIMIT 2;

ALTER ROLE analytics_sync_rw SET search_path = analytics, pg_catalog;

REVOKE ALL PRIVILEGES ON SCHEMA analytics FROM analytics_sync_rw;
GRANT USAGE ON SCHEMA analytics TO analytics_sync_rw;

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA analytics FROM analytics_sync_rw;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON ALL TABLES IN SCHEMA analytics
    TO analytics_sync_rw;

REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA analytics FROM analytics_sync_rw;

ALTER DEFAULT PRIVILEGES IN SCHEMA analytics
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO analytics_sync_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics
    REVOKE ALL ON SEQUENCES FROM analytics_sync_rw;
