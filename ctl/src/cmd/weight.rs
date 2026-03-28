use anyhow::{bail, Result};
use tokio_postgres::Client;

pub async fn list(client: &Client) -> Result<()> {
    let rows = client
        .query(
            "SELECT namespace, weight FROM namespace_weights ORDER BY namespace",
            &[],
        )
        .await?;

    if rows.is_empty() {
        println!("No weight overrides. All namespaces use default weight = 1.");
        return Ok(());
    }

    println!("{:<20} {}", "NAMESPACE", "WEIGHT");
    for row in &rows {
        let ns: &str = row.get(0);
        let weight: i32 = row.get(1);
        println!("{:<20} {}", ns, weight);
    }
    println!();
    println!("(Namespaces not listed above use default weight = 1)");
    Ok(())
}

pub async fn set(client: &Client, namespace: &str, weight: i32) -> Result<()> {
    if weight < 0 {
        bail!("Weight must be >= 0");
    }

    client
        .execute(
            "INSERT INTO namespace_weights (namespace, weight) VALUES ($1, $2) \
             ON CONFLICT (namespace) DO UPDATE SET weight = $2",
            &[&namespace, &weight],
        )
        .await?;

    println!("Set weight for '{}' to {}.", namespace, weight);
    Ok(())
}

pub async fn reset(client: &Client, namespace: &str) -> Result<()> {
    let count = client
        .execute(
            "DELETE FROM namespace_weights WHERE namespace = $1",
            &[&namespace],
        )
        .await?;

    if count == 0 {
        println!("No weight override found for '{}' (already default).", namespace);
    } else {
        println!("Reset weight for '{}' to default (1).", namespace);
    }
    Ok(())
}

pub async fn exclusive(client: &Client, namespace: &str, user_namespaces: &[String], label_selector: &str) -> Result<()> {
    if !user_namespaces.contains(&namespace.to_string()) {
        bail!(
            "Namespace '{}' not found in user namespaces (label {})",
            namespace,
            label_selector
        );
    }

    let mut disabled_count = 0u64;
    for ns in user_namespaces {
        if ns != namespace {
            client
                .execute(
                    "INSERT INTO namespace_weights (namespace, weight) VALUES ($1, 0) \
                     ON CONFLICT (namespace) DO UPDATE SET weight = 0",
                    &[&ns],
                )
                .await?;
            disabled_count += 1;
        }
    }

    // Ensure the exclusive namespace has default weight
    client
        .execute(
            "DELETE FROM namespace_weights WHERE namespace = $1",
            &[&namespace],
        )
        .await?;

    println!(
        "Exclusive mode: '{}' has the cluster. Disabled {} other namespace(s).",
        namespace, disabled_count
    );
    Ok(())
}

pub async fn release(client: &Client) -> Result<()> {
    eprint!("Release exclusive mode (delete all weight overrides)? [y/N] ");
    std::io::Write::flush(&mut std::io::stderr())?;
    let mut input = String::new();
    std::io::stdin().read_line(&mut input)?;
    if input.trim().to_lowercase() != "y" {
        println!("Aborted.");
        return Ok(());
    }

    let count = client
        .execute("DELETE FROM namespace_weights", &[])
        .await?;

    println!("Deleted {} weight override(s). All namespaces reset to default.", count);
    Ok(())
}
