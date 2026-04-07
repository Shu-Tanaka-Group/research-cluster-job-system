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
        ALTER TABLE jobs ADD COLUMN IF NOT EXISTS flavor TEXT NOT NULL DEFAULT 'cpu'; \
        ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cpu_millicores INTEGER; \
        ALTER TABLE jobs ADD COLUMN IF NOT EXISTS memory_mib INTEGER; \
        CREATE TABLE IF NOT EXISTS flavor_quotas ( \
            flavor TEXT PRIMARY KEY, \
            cpu TEXT NOT NULL, \
            memory TEXT NOT NULL, \
            gpu TEXT NOT NULL DEFAULT '0', \
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW() \
        ); \
        CREATE TABLE IF NOT EXISTS namespace_resource_quotas ( \
            namespace            TEXT PRIMARY KEY, \
            hard_cpu_millicores  INTEGER NOT NULL, \
            hard_memory_mib      INTEGER NOT NULL, \
            hard_gpu             INTEGER NOT NULL DEFAULT 0, \
            used_cpu_millicores  INTEGER NOT NULL, \
            used_memory_mib      INTEGER NOT NULL, \
            used_gpu             INTEGER NOT NULL DEFAULT 0, \
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW() \
        ); \
        ALTER TABLE namespace_resource_quotas ADD COLUMN IF NOT EXISTS hard_count INTEGER; \
        ALTER TABLE namespace_resource_quotas ADD COLUMN IF NOT EXISTS used_count INTEGER; \
        ALTER TABLE flavor_quotas ADD COLUMN IF NOT EXISTS drf_weight REAL NOT NULL DEFAULT 1.0; \
        ALTER TABLE namespace_daily_usage ADD COLUMN IF NOT EXISTS flavor TEXT NOT NULL DEFAULT 'cpu';";

    client
        .batch_execute(ddl)
        .await
        .context("Failed to run schema migration")?;

    // Change namespace_daily_usage PK to include flavor (idempotent)
    let pk_migration = "\
        DO $$ \
        BEGIN \
          IF NOT EXISTS ( \
            SELECT 1 FROM pg_constraint c \
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) \
            WHERE c.conname = 'namespace_daily_usage_pkey' AND a.attname = 'flavor' \
          ) THEN \
            ALTER TABLE namespace_daily_usage DROP CONSTRAINT namespace_daily_usage_pkey; \
            ALTER TABLE namespace_daily_usage ADD PRIMARY KEY (namespace, usage_date, flavor); \
          END IF; \
        END $$;";

    client
        .batch_execute(pk_migration)
        .await
        .context("Failed to migrate namespace_daily_usage PK")?;

    // Backfill cpu_millicores from cpu string column
    let backfill_cpu = "\
        UPDATE jobs SET cpu_millicores = CASE \
            WHEN cpu LIKE '%m' THEN CAST(REPLACE(cpu, 'm', '') AS INTEGER) \
            ELSE CAST(CEIL(CAST(cpu AS DOUBLE PRECISION) * 1000) AS INTEGER) \
        END \
        WHERE cpu_millicores IS NULL";

    // Backfill memory_mib from memory string column
    let backfill_mem = "\
        UPDATE jobs SET memory_mib = CASE \
            WHEN memory LIKE '%Gi' THEN CAST(CEIL(CAST(REPLACE(memory, 'Gi', '') AS DOUBLE PRECISION) * 1024) AS INTEGER) \
            WHEN memory LIKE '%Mi' THEN CAST(CEIL(CAST(REPLACE(memory, 'Mi', '') AS DOUBLE PRECISION)) AS INTEGER) \
            WHEN memory LIKE '%Ki' THEN CAST(CEIL(CAST(REPLACE(memory, 'Ki', '') AS DOUBLE PRECISION) / 1024) AS INTEGER) \
            ELSE CAST(CEIL(CAST(memory AS DOUBLE PRECISION) / (1024 * 1024)) AS INTEGER) \
        END \
        WHERE memory_mib IS NULL";

    client
        .batch_execute(backfill_cpu)
        .await
        .context("Failed to backfill cpu_millicores")?;
    client
        .batch_execute(backfill_mem)
        .await
        .context("Failed to backfill memory_mib")?;

    println!("Schema migration completed successfully.");
    Ok(())
}
