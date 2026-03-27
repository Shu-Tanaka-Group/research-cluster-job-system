use anyhow::Result;
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
