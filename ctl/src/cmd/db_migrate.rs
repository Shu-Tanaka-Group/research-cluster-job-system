use anyhow::{Context, Result};
use tokio_postgres::Client;

pub async fn migrate(client: &Client) -> Result<()> {
    let ddl = "\
        CREATE TABLE IF NOT EXISTS namespace_weights ( \
            namespace TEXT PRIMARY KEY, \
            weight    INTEGER NOT NULL DEFAULT 1 \
        ); \
        CREATE TABLE IF NOT EXISTS namespace_daily_usage ( \
            namespace              TEXT NOT NULL, \
            usage_date             DATE NOT NULL, \
            cpu_millicores_seconds BIGINT NOT NULL DEFAULT 0, \
            memory_mib_seconds     BIGINT NOT NULL DEFAULT 0, \
            gpu_seconds            BIGINT NOT NULL DEFAULT 0, \
            PRIMARY KEY (namespace, usage_date) \
        ); \
        CREATE TABLE IF NOT EXISTS node_resources ( \
            node_name           TEXT PRIMARY KEY, \
            cpu_millicores      INTEGER NOT NULL, \
            memory_mib          INTEGER NOT NULL, \
            gpu                 INTEGER NOT NULL DEFAULT 0, \
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW() \
        ); \
        ALTER TABLE node_resources ADD COLUMN IF NOT EXISTS flavor TEXT NOT NULL DEFAULT 'cpu'; \
        ALTER TABLE jobs ADD COLUMN IF NOT EXISTS time_limit_seconds INTEGER NOT NULL DEFAULT 86400; \
        ALTER TABLE jobs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ; \
        ALTER TABLE jobs ADD COLUMN IF NOT EXISTS completions INTEGER; \
        ALTER TABLE jobs ADD COLUMN IF NOT EXISTS parallelism INTEGER; \
        ALTER TABLE jobs ADD COLUMN IF NOT EXISTS completed_indexes TEXT; \
        ALTER TABLE jobs ADD COLUMN IF NOT EXISTS failed_indexes TEXT; \
        ALTER TABLE jobs ADD COLUMN IF NOT EXISTS succeeded_count INTEGER; \
        ALTER TABLE jobs ADD COLUMN IF NOT EXISTS failed_count INTEGER; \
        ALTER TABLE jobs ADD COLUMN IF NOT EXISTS node_name TEXT; \
        ALTER TABLE jobs ADD COLUMN IF NOT EXISTS flavor TEXT NOT NULL DEFAULT 'cpu';";

    client
        .batch_execute(ddl)
        .await
        .context("Failed to run schema migration")?;

    println!("Schema migration completed successfully.");
    Ok(())
}
