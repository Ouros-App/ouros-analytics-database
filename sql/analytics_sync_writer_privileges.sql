-- The sync login is provisioned outside this repository because its password
-- belongs to the runtime secret manager. Keep only schema/table privileges here.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'analytics_sync_rw'
    ) THEN
        EXECUTE 'GRANT USAGE ON SCHEMA analytics TO analytics_sync_rw';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA analytics TO analytics_sync_rw';
        EXECUTE 'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA analytics FROM analytics_sync_rw';
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO analytics_sync_rw';
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA analytics REVOKE ALL ON SEQUENCES FROM analytics_sync_rw';
    END IF;
END
$$;
