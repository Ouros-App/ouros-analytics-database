CREATE TABLE IF NOT EXISTS analytics.sync_state (
    sync_name TEXT PRIMARY KEY,
    last_sync TIMESTAMPTZ NOT NULL DEFAULT '1970-01-01 00:00:00+00',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

COMMENT ON TABLE analytics.sync_state IS
    'Watermark transacional da sincronizacao incremental da origem de producao.';
