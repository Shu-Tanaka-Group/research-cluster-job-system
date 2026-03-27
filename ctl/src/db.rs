use anyhow::{Context, Result};
use tokio_postgres::{Client, NoTls};

use crate::config::DatabaseConfig;

pub async fn connect(config: &DatabaseConfig) -> Result<Client> {
    let mut pg_config = tokio_postgres::Config::new();
    pg_config
        .host(&config.host)
        .port(config.port)
        .dbname(&config.database)
        .user(&config.user)
        .password(&config.password);

    let (client, connection) = pg_config
        .connect(NoTls)
        .await
        .context("Failed to connect to database")?;

    tokio::spawn(async move {
        if let Err(e) = connection.await {
            eprintln!("Database connection error: {}", e);
        }
    });

    Ok(client)
}
