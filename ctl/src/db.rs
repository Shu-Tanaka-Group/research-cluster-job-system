use anyhow::{Context, Result};
use std::process::Stdio;
use tokio::io::AsyncBufReadExt;
use tokio::process::{Child, Command};
use tokio_postgres::{Client, NoTls};

use crate::config::DatabaseConfig;

/// A database connection with an associated port-forward process.
/// The port-forward process is killed when this struct is dropped.
pub struct DbConnection {
    pub client: Client,
    _port_forward: Child,
}

impl Drop for DbConnection {
    fn drop(&mut self) {
        // Kill the port-forward process on cleanup
        let _ = self._port_forward.start_kill();
    }
}

/// Start kubectl port-forward on a random local port and connect to the database.
pub async fn connect(config: &DatabaseConfig, k8s_namespace: &str) -> Result<DbConnection> {
    // Use port 0 to let the OS assign a random available port
    let mut child = Command::new("kubectl")
        .args([
            "port-forward",
            "-n",
            k8s_namespace,
            "svc/postgres",
            "0:5432",
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .context("Failed to start kubectl port-forward. Is kubectl installed and in PATH?")?;

    // Read stdout to discover the assigned local port.
    // kubectl prints: "Forwarding from 127.0.0.1:<port> -> 5432"
    let stdout = child
        .stdout
        .take()
        .context("Failed to capture port-forward stdout")?;

    let mut reader = tokio::io::BufReader::new(stdout);
    let mut line = String::new();

    let local_port = tokio::time::timeout(std::time::Duration::from_secs(10), async {
        loop {
            line.clear();
            let n = reader
                .read_line(&mut line)
                .await
                .context("Failed to read port-forward output")?;
            if n == 0 {
                anyhow::bail!("kubectl port-forward exited unexpectedly");
            }
            // Parse "Forwarding from 127.0.0.1:12345 -> 5432"
            if let Some(port) = parse_forwarded_port(&line) {
                return Ok(port);
            }
        }
    })
    .await
    .context("Timed out waiting for port-forward to start")?
    .context("Failed to establish port-forward")?;

    // Connect to the database via the forwarded port
    let mut pg_config = tokio_postgres::Config::new();
    pg_config
        .host("127.0.0.1")
        .port(local_port)
        .dbname(&config.database)
        .user(&config.user)
        .password(&config.password);

    let (client, connection) = pg_config
        .connect(NoTls)
        .await
        .context("Failed to connect to database via port-forward")?;

    tokio::spawn(async move {
        if let Err(e) = connection.await {
            eprintln!("Database connection error: {}", e);
        }
    });

    Ok(DbConnection {
        client,
        _port_forward: child,
    })
}

/// Parse the local port from kubectl port-forward output.
/// Expected format: "Forwarding from 127.0.0.1:12345 -> 5432"
///                   "Forwarding from [::1]:12345 -> 5432"
fn parse_forwarded_port(line: &str) -> Option<u16> {
    if !line.contains("Forwarding from") {
        return None;
    }
    // Find the port before " -> "
    let arrow_pos = line.find(" -> ")?;
    let before_arrow = &line[..arrow_pos];
    let colon_pos = before_arrow.rfind(':')?;
    before_arrow[colon_pos + 1..].parse().ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_forwarded_port_ipv4() {
        let line = "Forwarding from 127.0.0.1:12345 -> 5432";
        assert_eq!(parse_forwarded_port(line), Some(12345));
    }

    #[test]
    fn test_parse_forwarded_port_ipv6() {
        let line = "Forwarding from [::1]:54321 -> 5432";
        assert_eq!(parse_forwarded_port(line), Some(54321));
    }

    #[test]
    fn test_parse_forwarded_port_unrelated() {
        assert_eq!(parse_forwarded_port("some other output"), None);
    }
}
