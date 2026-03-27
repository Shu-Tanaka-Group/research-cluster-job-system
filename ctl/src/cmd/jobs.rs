use anyhow::{bail, Result};
use std::io::{self, Write};
use tokio_postgres::Client;

pub async fn list(client: &Client, namespace: Option<&str>, status: Option<&str>) -> Result<()> {
    let rows = client
        .query(
            "SELECT namespace, job_id, status, command, created_at, started_at, finished_at \
             FROM jobs \
             WHERE ($1::TEXT IS NULL OR namespace = $1) \
               AND ($2::TEXT IS NULL OR status = $2) \
             ORDER BY namespace, job_id",
            &[&namespace, &status],
        )
        .await?;

    if rows.is_empty() {
        println!("No jobs found.");
        return Ok(());
    }

    println!(
        "{:<20} {:<8} {:<12} {:<40} {:<20} {}",
        "NAMESPACE", "JOB_ID", "STATUS", "COMMAND", "CREATED", "FINISHED"
    );
    for row in &rows {
        let ns: &str = row.get(0);
        let job_id: i32 = row.get(1);
        let status: &str = row.get(2);
        let command: &str = row.get(3);
        let created_at: chrono::DateTime<chrono::Utc> = row.get(4);
        let finished_at: Option<chrono::DateTime<chrono::Utc>> = row.get(6);

        let cmd_display = if command.len() > 40 {
            format!("{}...", &command[..37])
        } else {
            command.to_string()
        };
        let created = created_at.format("%Y-%m-%dT%H:%M").to_string();
        let finished = finished_at
            .map(|t| t.format("%Y-%m-%dT%H:%M").to_string())
            .unwrap_or_else(|| "-".to_string());

        println!(
            "{:<20} {:<8} {:<12} {:<40} {:<20} {}",
            ns, job_id, status, cmd_display, created, finished
        );
    }
    Ok(())
}

pub async fn stalled(client: &Client) -> Result<()> {
    let rows = client
        .query(
            "SELECT namespace, job_id, dispatched_at, \
               EXTRACT(EPOCH FROM NOW() - dispatched_at)::INT AS elapsed_sec \
             FROM jobs \
             WHERE status = 'DISPATCHED' \
             ORDER BY dispatched_at",
            &[],
        )
        .await?;

    if rows.is_empty() {
        println!("No stalled jobs.");
        return Ok(());
    }

    println!(
        "{:<20} {:<8} {:<24} {}",
        "NAMESPACE", "JOB_ID", "DISPATCHED_AT", "ELAPSED"
    );
    for row in &rows {
        let ns: &str = row.get(0);
        let job_id: i32 = row.get(1);
        let dispatched_at: chrono::DateTime<chrono::Utc> = row.get(2);
        let elapsed_sec: i32 = row.get(3);

        println!(
            "{:<20} {:<8} {:<24} {}s",
            ns,
            job_id,
            dispatched_at.format("%Y-%m-%dT%H:%M:%S"),
            elapsed_sec
        );
    }
    Ok(())
}

pub async fn remaining(client: &Client) -> Result<()> {
    let rows = client
        .query(
            "SELECT namespace, job_id, command, time_limit_seconds, started_at, \
               EXTRACT(EPOCH FROM \
                 (started_at + MAKE_INTERVAL(secs => time_limit_seconds)) - NOW() \
               )::INT AS remaining_sec \
             FROM jobs \
             WHERE status = 'RUNNING' AND started_at IS NOT NULL \
             ORDER BY remaining_sec",
            &[],
        )
        .await?;

    if rows.is_empty() {
        println!("No running jobs.");
        return Ok(());
    }

    println!(
        "{:<20} {:<8} {:<30} {:<12} {}",
        "NAMESPACE", "JOB_ID", "COMMAND", "TIME_LIMIT", "REMAINING"
    );
    for row in &rows {
        let ns: &str = row.get(0);
        let job_id: i32 = row.get(1);
        let command: &str = row.get(2);
        let time_limit: i32 = row.get(3);
        let remaining: i32 = row.get(4);

        let cmd_display = if command.len() > 30 {
            format!("{}...", &command[..27])
        } else {
            command.to_string()
        };

        println!(
            "{:<20} {:<8} {:<30} {:<12} {}",
            ns,
            job_id,
            cmd_display,
            format_duration(time_limit),
            format_duration(remaining.max(0))
        );
    }
    Ok(())
}

pub async fn summary(client: &Client) -> Result<()> {
    let rows = client
        .query(
            "SELECT namespace, status, COUNT(*)::INT AS count \
             FROM jobs \
             GROUP BY namespace, status \
             ORDER BY namespace, status",
            &[],
        )
        .await?;

    if rows.is_empty() {
        println!("No jobs found.");
        return Ok(());
    }

    // Collect all statuses and namespaces
    let mut statuses: Vec<String> = Vec::new();
    let mut namespaces: Vec<String> = Vec::new();
    let mut data: std::collections::HashMap<(String, String), i32> = std::collections::HashMap::new();

    for row in &rows {
        let ns: String = row.get(0);
        let status: String = row.get(1);
        let count: i32 = row.get(2);

        if !namespaces.contains(&ns) {
            namespaces.push(ns.clone());
        }
        if !statuses.contains(&status) {
            statuses.push(status.clone());
        }
        data.insert((ns, status), count);
    }

    // Print pivot table
    print!("{:<20}", "NAMESPACE");
    for s in &statuses {
        print!(" {:>10}", s);
    }
    println!();

    for ns in &namespaces {
        print!("{:<20}", ns);
        for s in &statuses {
            let count = data.get(&(ns.clone(), s.clone())).unwrap_or(&0);
            print!(" {:>10}", count);
        }
        println!();
    }
    Ok(())
}

pub async fn cancel(
    client: &Client,
    namespace: &str,
    job_id: Option<i32>,
    status: Option<&str>,
    all: bool,
) -> Result<()> {
    // Validate arguments: exactly one of --job-id, --status, or --all must be specified
    let specified = [job_id.is_some(), status.is_some(), all]
        .iter()
        .filter(|&&v| v)
        .count();
    if specified != 1 {
        bail!("Specify exactly one of --job-id, --status, or --all");
    }

    let cancellable_statuses = ["QUEUED", "DISPATCHING", "DISPATCHED", "RUNNING"];

    // Find target jobs
    let rows = if let Some(id) = job_id {
        client
            .query(
                "SELECT job_id, status FROM jobs \
                 WHERE namespace = $1 AND job_id = $2",
                &[&namespace, &id],
            )
            .await?
    } else if let Some(s) = status {
        if !cancellable_statuses.contains(&s) {
            bail!(
                "Cannot cancel jobs with status '{}'. Cancellable statuses: {}",
                s,
                cancellable_statuses.join(", ")
            );
        }
        client
            .query(
                "SELECT job_id, status FROM jobs \
                 WHERE namespace = $1 AND status = $2 \
                 ORDER BY job_id",
                &[&namespace, &s],
            )
            .await?
    } else {
        // --all
        client
            .query(
                "SELECT job_id, status FROM jobs \
                 WHERE namespace = $1 AND status = ANY($2) \
                 ORDER BY job_id",
                &[&namespace, &cancellable_statuses.as_slice()],
            )
            .await?
    };

    if rows.is_empty() {
        println!("No matching jobs found.");
        return Ok(());
    }

    // Filter to cancellable jobs and report skipped ones
    let mut targets: Vec<i32> = Vec::new();
    for row in &rows {
        let id: i32 = row.get(0);
        let st: &str = row.get(1);
        if cancellable_statuses.contains(&st) {
            targets.push(id);
        } else {
            println!("Skipping job {} (status: {})", id, st);
        }
    }

    if targets.is_empty() {
        println!("No cancellable jobs found.");
        return Ok(());
    }

    // Confirmation prompt
    println!(
        "Will cancel {} job(s) in namespace '{}':",
        targets.len(),
        namespace
    );
    for id in &targets {
        println!("  job_id: {}", id);
    }
    print!("Proceed? [y/N] ");
    io::stdout().flush()?;
    let mut input = String::new();
    io::stdin().read_line(&mut input)?;
    if !input.trim().eq_ignore_ascii_case("y") {
        println!("Cancelled.");
        return Ok(());
    }

    // Execute cancel
    let updated = client
        .execute(
            "UPDATE jobs SET status = 'CANCELLED' \
             WHERE namespace = $1 AND job_id = ANY($2) \
               AND status = ANY($3)",
            &[&namespace, &targets, &cancellable_statuses.as_slice()],
        )
        .await?;

    println!("{} job(s) cancelled.", updated);
    Ok(())
}

fn format_duration(secs: i32) -> String {
    if secs < 0 {
        return "0s".to_string();
    }
    let secs = secs as u32;
    let days = secs / 86400;
    let hours = (secs % 86400) / 3600;
    let minutes = (secs % 3600) / 60;
    if days > 0 {
        format!("{}d {}h", days, hours)
    } else if hours > 0 {
        format!("{}h {}m", hours, minutes)
    } else {
        format!("{}m", minutes)
    }
}
