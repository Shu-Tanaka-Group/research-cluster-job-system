use anyhow::{bail, Result};
use tokio_postgres::Client;

pub async fn list(client: &Client, cluster_totals: &ClusterTotals, namespace: Option<&str>) -> Result<()> {
    // 1. Daily raw data
    let daily_rows = client
        .query(
            "SELECT namespace, usage_date, cpu_millicores_seconds, memory_mib_seconds, gpu_seconds \
             FROM namespace_daily_usage \
             WHERE ($1::TEXT IS NULL OR namespace = $1) \
             ORDER BY usage_date ASC, namespace ASC",
            &[&namespace],
        )
        .await?;

    if daily_rows.is_empty() {
        println!("No usage data found.");
        return Ok(());
    }

    println!("=== Daily Usage ===");
    println!(
        "{:<20} {:<12} {:>14} {:>14} {:>10}",
        "NAMESPACE", "DATE", "CPU (core·h)", "Mem (GiB·h)", "GPU (h)"
    );
    for row in &daily_rows {
        let ns: &str = row.get(0);
        let date: chrono::NaiveDate = row.get(1);
        let cpu: i64 = row.get(2);
        let mem: i64 = row.get(3);
        let gpu: i64 = row.get(4);
        println!(
            "{:<20} {:<12} {:>14.1} {:>14.1} {:>10.1}",
            ns,
            date,
            cpu as f64 / 1000.0 / 3600.0,
            mem as f64 / 1024.0 / 3600.0,
            gpu as f64 / 3600.0,
        );
    }

    // 2. 7-day window aggregate
    println!();
    println!("=== 7-Day Window Aggregate ===");
    let window_rows = client
        .query(
            "SELECT namespace, \
               SUM(cpu_millicores_seconds)::BIGINT AS cpu, \
               SUM(memory_mib_seconds)::BIGINT AS mem, \
               SUM(gpu_seconds)::BIGINT AS gpu \
             FROM namespace_daily_usage \
             WHERE usage_date > CURRENT_DATE - 7 \
               AND ($1::TEXT IS NULL OR namespace = $1) \
             GROUP BY namespace ORDER BY namespace",
            &[&namespace],
        )
        .await?;

    println!(
        "{:<20} {:>14} {:>14} {:>10}",
        "NAMESPACE", "CPU (core·h)", "Mem (GiB·h)", "GPU (h)"
    );
    for row in &window_rows {
        let ns: &str = row.get(0);
        let cpu: i64 = row.get(1);
        let mem: i64 = row.get(2);
        let gpu: i64 = row.get(3);
        println!(
            "{:<20} {:>14.1} {:>14.1} {:>10.1}",
            ns,
            cpu as f64 / 1000.0 / 3600.0,
            mem as f64 / 1024.0 / 3600.0,
            gpu as f64 / 3600.0,
        );
    }

    // 3. DRF dominant share
    println!();
    println!("=== DRF Dominant Share ===");
    let cluster_cpu = cluster_totals.cpu_millicores as f64;
    let cluster_mem = cluster_totals.memory_mib as f64;
    let cluster_gpus_f = cluster_totals.gpus as f64;

    let drf_query = format!(
        "SELECT \
           u.namespace, \
           SUM(u.cpu_millicores_seconds)::BIGINT AS cpu_total, \
           SUM(u.memory_mib_seconds)::BIGINT AS mem_total, \
           SUM(u.gpu_seconds)::BIGINT AS gpu_total, \
           COALESCE(w.weight, 1) AS weight, \
           GREATEST( \
             COALESCE(SUM(u.cpu_millicores_seconds), 0) * 1.0 / {}, \
             COALESCE(SUM(u.memory_mib_seconds), 0) * 1.0 / {}, \
             COALESCE(SUM(u.gpu_seconds), 0) * 1.0 / NULLIF({}, 0) \
           ) / COALESCE(w.weight, 1) AS weighted_dominant_share \
         FROM namespace_daily_usage u \
         LEFT JOIN namespace_weights w ON u.namespace = w.namespace \
         WHERE u.usage_date > CURRENT_DATE - 7 \
           AND ($1::TEXT IS NULL OR u.namespace = $1) \
         GROUP BY u.namespace, w.weight \
         ORDER BY weighted_dominant_share ASC NULLS FIRST",
        cluster_totals.cpu_millicores, cluster_totals.memory_mib, cluster_totals.gpus
    );
    let drf_rows = client.query(&drf_query, &[&namespace]).await?;

    println!(
        "{:<20} {:>14} {:>14} {:>10} {:>8} {:>16}",
        "NAMESPACE", "CPU (core·h)", "Mem (GiB·h)", "GPU (h)", "WEIGHT", "DOM_SHARE"
    );
    for row in &drf_rows {
        let ns: &str = row.get(0);
        let cpu: i64 = row.get(1);
        let mem: i64 = row.get(2);
        let gpu: i64 = row.get(3);
        let weight: i32 = row.get(4);

        // Compute dominant share locally for display
        let cpu_share = cpu as f64 / cluster_cpu;
        let mem_share = mem as f64 / cluster_mem;
        let gpu_share = if cluster_gpus_f > 0.0 {
            gpu as f64 / cluster_gpus_f
        } else {
            0.0
        };
        let dom_share = cpu_share.max(mem_share).max(gpu_share);
        let weighted = if weight > 0 {
            dom_share / weight as f64
        } else {
            f64::INFINITY
        };

        println!(
            "{:<20} {:>14.1} {:>14.1} {:>10.1} {:>8} {:>16.6}",
            ns,
            cpu as f64 / 1000.0 / 3600.0,
            mem as f64 / 1024.0 / 3600.0,
            gpu as f64 / 3600.0,
            weight,
            weighted,
        );
    }

    Ok(())
}

pub async fn reset(client: &Client, namespace: Option<&str>, all: bool) -> Result<()> {
    if !all && namespace.is_none() {
        bail!("Specify --namespace <ns> or --all");
    }

    let target = if all {
        "all namespaces".to_string()
    } else {
        namespace.unwrap().to_string()
    };

    eprint!("Delete usage data for {}. Continue? [y/N] ", target);
    std::io::Write::flush(&mut std::io::stderr())?;
    let mut input = String::new();
    std::io::stdin().read_line(&mut input)?;
    if input.trim().to_lowercase() != "y" {
        println!("Aborted.");
        return Ok(());
    }

    let count = if all {
        client
            .execute("DELETE FROM namespace_daily_usage", &[])
            .await?
    } else {
        client
            .execute(
                "DELETE FROM namespace_daily_usage WHERE namespace = $1",
                &[&namespace.unwrap()],
            )
            .await?
    };

    println!("Deleted {} row(s).", count);
    Ok(())
}

pub struct ClusterTotals {
    pub cpu_millicores: i64,
    pub memory_mib: i64,
    pub gpus: i64,
}

impl Default for ClusterTotals {
    fn default() -> Self {
        Self {
            cpu_millicores: 256000,
            memory_mib: 1024000,
            gpus: 0,
        }
    }
}

impl ClusterTotals {
    pub async fn from_db(client: &Client) -> Self {
        match client
            .query_one(
                "SELECT COALESCE(SUM(cpu_millicores), 0)::BIGINT, \
                        COALESCE(SUM(memory_mib), 0)::BIGINT, \
                        COALESCE(SUM(gpu), 0)::BIGINT \
                 FROM node_resources",
                &[],
            )
            .await
        {
            Ok(row) => {
                let cpu: i64 = row.get(0);
                let mem: i64 = row.get(1);
                let gpus: i64 = row.get(2);
                if cpu == 0 && mem == 0 {
                    eprintln!(
                        "Warning: node_resources is empty. Using default cluster totals."
                    );
                    Self::default()
                } else {
                    Self {
                        cpu_millicores: cpu,
                        memory_mib: mem,
                        gpus,
                    }
                }
            }
            Err(e) => {
                eprintln!(
                    "Warning: Could not query node_resources ({}). Using defaults.",
                    e
                );
                Self::default()
            }
        }
    }

    /// Fetch allocatable totals for nodes matching a specific DB flavor value.
    /// Falls back to cluster-wide totals if no nodes match the given flavor.
    pub async fn from_db_by_flavor(client: &Client, flavor: &str) -> Self {
        match client
            .query_one(
                "SELECT COALESCE(SUM(cpu_millicores), 0)::BIGINT, \
                        COALESCE(SUM(memory_mib), 0)::BIGINT, \
                        COALESCE(SUM(gpu), 0)::BIGINT \
                 FROM node_resources WHERE flavor = $1",
                &[&flavor],
            )
            .await
        {
            Ok(row) => {
                let cpu: i64 = row.get(0);
                let mem: i64 = row.get(1);
                let gpus: i64 = row.get(2);
                if cpu == 0 && mem == 0 {
                    eprintln!(
                        "Warning: No nodes with flavor '{}' found. Falling back to cluster totals.",
                        flavor,
                    );
                    Self::from_db(client).await
                } else {
                    Self {
                        cpu_millicores: cpu,
                        memory_mib: mem,
                        gpus,
                    }
                }
            }
            Err(e) => {
                eprintln!(
                    "Warning: Could not query node_resources by flavor ({}). Using cluster totals.",
                    e,
                );
                Self::from_db(client).await
            }
        }
    }
}
