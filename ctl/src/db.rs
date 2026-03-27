use anyhow::{Context, Result};
use tokio_postgres::{Client, NoTls};

use crate::config::DatabaseConfig;

pub async fn connect(config: &DatabaseConfig) -> Result<Client> {
    let conn_str = format!(
        "host={} port={} dbname={} user={} password={}",
        config.host, config.port, config.database, config.user, config.password
    );
    let (client, connection) = tokio_postgres::connect(&conn_str, NoTls)
        .await
        .context("Failed to connect to database")?;

    tokio::spawn(async move {
        if let Err(e) = connection.await {
            eprintln!("Database connection error: {}", e);
        }
    });

    Ok(client)
}
