DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'analytics_ro'
    ) THEN
        CREATE ROLE analytics_ro
            LOGIN
            NOINHERIT
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION
            NOBYPASSRLS
            CONNECTION LIMIT 5;
    END IF;
END
$$;

ALTER ROLE analytics_ro
    LOGIN
    NOINHERIT
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOREPLICATION
    NOBYPASSRLS
    CONNECTION LIMIT 5;

ALTER ROLE analytics_ro SET default_transaction_read_only = on;
ALTER ROLE analytics_ro SET search_path = analytics, pg_catalog;

REVOKE ALL PRIVILEGES ON SCHEMA analytics FROM PUBLIC;
GRANT USAGE ON SCHEMA analytics TO analytics_ro;

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA analytics FROM analytics_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO analytics_ro;

REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA analytics FROM analytics_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics
    REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics
    GRANT SELECT ON TABLES TO analytics_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics
    REVOKE ALL ON SEQUENCES FROM PUBLIC;
