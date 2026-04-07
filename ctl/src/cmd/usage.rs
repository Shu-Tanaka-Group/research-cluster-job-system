use std::collections::HashMap;

use anyhow::{bail, Result};
use tokio_postgres::Client;

use super::cluster::parse_resource_quantity;

struct FlavorCap {
    cpu: f64,
    mem: f64,
    gpu: f64,
    weight: f64,
}

async fn fetch_flavor_caps(client: &Client) -> HashMap<String, FlavorCap> {
    let alloc_rows = match client
        .query(
            "SELECT flavor, \
                    COALESCE(SUM(cpu_millicores), 0)::BIGINT AS total_cpu, \
                    COALESCE(SUM(memory_mib), 0)::BIGINT AS total_mem, \
                    COALESCE(SUM(gpu), 0)::BIGINT AS total_gpu \
             FROM node_resources GROUP BY flavor",
            &[],
        )
        .await
    {
        Ok(rows) => rows,
        Err(e) => {
            eprintln!("Warning: Could not query node_resources ({}). DRF disabled.", e);
            return HashMap::new();
        }
    };

    if alloc_rows.is_empty() {
        return HashMap::new();
    }

    // Per-flavor nominalQuota and drf_weight
    let quota_rows = match client
        .query("SELECT flavor, cpu, memory, gpu, drf_weight FROM flavor_quotas", &[])
        .await
    {
        Ok(rows) => rows,
        Err(_) => Vec::new(),
    };

    let mut quotas: HashMap<String, (f64, f64, f64, f64)> = HashMap::new();
    for row in &quota_rows {
        let flavor: &str = row.get(0);
        let cpu_str: &str = row.get(1);
        let mem_str: &str = row.get(2);
        let gpu_str: &str = row.get(3);
        let weight: f32 = row.get(4);
        quotas.insert(
            flavor.to_string(),
            (
                parse_resource_quantity(cpu_str) * 1000.0,    // cores → millicores
                parse_resource_quantity(mem_str) / 1048576.0,  // bytes → MiB
                parse_resource_quantity(gpu_str),
                weight as f64,
            ),
        );
    }

    let mut caps = HashMap::new();
    for row in &alloc_rows {
        let flavor: &str = row.get(0);
        let alloc_cpu: i64 = row.get(1);
        let alloc_mem: i64 = row.get(2);
        let alloc_gpu: i64 = row.get(3);

        if let Some((q_cpu, q_mem, q_gpu, weight)) = quotas.get(flavor) {
            caps.insert(
                flavor.to_string(),
                FlavorCap {
                    cpu: (alloc_cpu as f64).min(*q_cpu),
                    mem: (alloc_mem as f64).min(*q_mem),
                    gpu: (alloc_gpu as f64).min(*q_gpu),
                    weight: *weight,
                },
            );
        } else {
            caps.insert(
                flavor.to_string(),
                FlavorCap {
                    cpu: alloc_cpu as f64,
                    mem: alloc_mem as f64,
                    gpu: alloc_gpu as f64,
                    weight: 1.0,
                },
            );
        }
    }

    caps
}

pub async fn list(client: &Client, namespace: Option<&str>) -> Result<()> {
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

    // 3. DRF dominant share (per-flavor method)
    println!();
    println!("=== DRF Dominant Share ===");

    let flavor_caps = fetch_flavor_caps(client).await;
    if flavor_caps.is_empty() {
        println!("No node_resources data. DRF disabled.");
        return Ok(());
    }

    // Fetch per-(namespace, flavor) consumption
    let usage_rows = client
        .query(
            "SELECT u.namespace, u.flavor, \
               SUM(u.cpu_millicores_seconds)::BIGINT AS cpu, \
               SUM(u.memory_mib_seconds)::BIGINT AS mem, \
               SUM(u.gpu_seconds)::BIGINT AS gpu \
             FROM namespace_daily_usage u \
             WHERE u.usage_date > CURRENT_DATE - 7 \
               AND ($1::TEXT IS NULL OR u.namespace = $1) \
             GROUP BY u.namespace, u.flavor \
             ORDER BY u.namespace, u.flavor",
            &[&namespace],
        )
        .await?;

    // Fetch namespace weights
    let weight_rows = client
        .query("SELECT namespace, weight FROM namespace_weights", &[])
        .await?;
    let mut ns_weights: HashMap<String, i32> = HashMap::new();
    for row in &weight_rows {
        let ns: &str = row.get(0);
        let w: i32 = row.get(1);
        ns_weights.insert(ns.to_string(), w);
    }

    // Aggregate per namespace: total consumption + per-flavor DRF score
    struct NsData {
        total_cpu: i64,
        total_mem: i64,
        total_gpu: i64,
        drf_score: f64,
    }
    let mut ns_data: HashMap<String, NsData> = HashMap::new();

    for row in &usage_rows {
        let ns: &str = row.get(0);
        let flavor: &str = row.get(1);
        let cpu: i64 = row.get(2);
        let mem: i64 = row.get(3);
        let gpu: i64 = row.get(4);

        let entry = ns_data.entry(ns.to_string()).or_insert(NsData {
            total_cpu: 0,
            total_mem: 0,
            total_gpu: 0,
            drf_score: 0.0,
        });

        entry.total_cpu += cpu;
        entry.total_mem += mem;
        entry.total_gpu += gpu;

        // Compute per-flavor dominant share
        if let Some(cap) = flavor_caps.get(flavor) {
            let cpu_share = if cap.cpu > 0.0 { cpu as f64 / cap.cpu } else { 0.0 };
            let mem_share = if cap.mem > 0.0 { mem as f64 / cap.mem } else { 0.0 };
            let gpu_share = if cap.gpu > 0.0 { gpu as f64 / cap.gpu } else { 0.0 };
            let dom_share = cpu_share.max(mem_share).max(gpu_share);
            entry.drf_score += dom_share * cap.weight;
        }
    }

    // Sort by weighted DRF score ascending
    let mut ns_list: Vec<(String, &NsData)> = ns_data.iter().map(|(k, v)| (k.clone(), v)).collect();
    ns_list.sort_by(|a, b| {
        let w_a = *ns_weights.get(&a.0).unwrap_or(&1) as f64;
        let w_b = *ns_weights.get(&b.0).unwrap_or(&1) as f64;
        let score_a = if w_a > 0.0 { a.1.drf_score / w_a } else { f64::INFINITY };
        let score_b = if w_b > 0.0 { b.1.drf_score / w_b } else { f64::INFINITY };
        score_a.partial_cmp(&score_b).unwrap_or(std::cmp::Ordering::Equal)
    });

    println!(
        "{:<20} {:>14} {:>14} {:>10} {:>8} {:>16}",
        "NAMESPACE", "CPU (core·h)", "Mem (GiB·h)", "GPU (h)", "WEIGHT", "DOM_SHARE"
    );
    for (ns, data) in &ns_list {
        let weight = *ns_weights.get(ns.as_str()).unwrap_or(&1);
        let weighted = if weight > 0 {
            data.drf_score / weight as f64
        } else {
            f64::INFINITY
        };

        println!(
            "{:<20} {:>14.1} {:>14.1} {:>10.1} {:>8} {:>16.6}",
            ns,
            data.total_cpu as f64 / 1000.0 / 3600.0,
            data.total_mem as f64 / 1024.0 / 3600.0,
            data.total_gpu as f64 / 3600.0,
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

pub async fn quota(client: &Client, user_namespaces: &[String], namespace: Option<&str>) -> Result<()> {
    // Filter user namespaces if --namespace is specified
    let targets: Vec<&str> = if let Some(ns) = namespace {
        if user_namespaces.iter().any(|n| n == ns) {
            vec![ns]
        } else {
            println!("Namespace '{}' not found in user namespaces.", ns);
            return Ok(());
        }
    } else {
        let mut ns: Vec<&str> = user_namespaces.iter().map(|s| s.as_str()).collect();
        ns.sort();
        ns
    };

    if targets.is_empty() {
        println!("No user namespaces found.");
        return Ok(());
    }

    // Fetch all ResourceQuota rows from DB
    let rows = client
        .query(
            "SELECT namespace, hard_cpu_millicores, hard_memory_mib, hard_gpu, \
                    used_cpu_millicores, used_memory_mib, used_gpu, updated_at, \
                    hard_count, used_count \
             FROM namespace_resource_quotas \
             ORDER BY namespace",
            &[],
        )
        .await?;

    let mut quota_map = std::collections::HashMap::new();
    for row in &rows {
        let ns: &str = row.get(0);
        quota_map.insert(ns.to_string(), row);
    }

    let now = chrono::Utc::now();

    let headers = ["Namespace", "CPU (used/hard)", "Memory (used/hard)", "GPU (used/hard)", "Jobs (used/hard)", "Updated"];

    // Pass 1: collect formatted data
    let mut table_rows: Vec<[String; 6]> = Vec::new();
    for ns in &targets {
        if let Some(row) = quota_map.get(*ns) {
            let hard_cpu: i32 = row.get(1);
            let hard_mem: i32 = row.get(2);
            let hard_gpu: i32 = row.get(3);
            let used_cpu: i32 = row.get(4);
            let used_mem: i32 = row.get(5);
            let used_gpu: i32 = row.get(6);
            let updated_at: chrono::DateTime<chrono::Utc> = row.get(7);
            let hard_count: Option<i32> = row.get(8);
            let used_count: Option<i32> = row.get(9);

            let count_str = match (hard_count, used_count) {
                (Some(h), Some(u)) => format!("{} / {}", u, h),
                _ => "-".to_string(),
            };

            table_rows.push([
                ns.to_string(),
                format!("{:.1} / {:.1}", used_cpu as f64 / 1000.0, hard_cpu as f64 / 1000.0),
                format!("{}Gi / {}Gi", used_mem / 1024, hard_mem / 1024),
                format!("{} / {}", used_gpu, hard_gpu),
                count_str,
                format_age(now - updated_at),
            ]);
        } else {
            table_rows.push([
                ns.to_string(),
                "-".to_string(),
                "-".to_string(),
                "-".to_string(),
                "-".to_string(),
                "-".to_string(),
            ]);
        }
    }

    // Calculate dynamic column widths
    let mut widths: Vec<usize> = headers.iter().map(|h| h.len()).collect();
    for row in &table_rows {
        for (i, cell) in row.iter().enumerate() {
            widths[i] = widths[i].max(cell.len());
        }
    }

    // Pass 2: print header and rows
    let sep = "   ";
    for (i, h) in headers.iter().enumerate() {
        if i > 0 { print!("{}", sep); }
        if i == headers.len() - 1 {
            print!("{}", h);
        } else {
            print!("{:<width$}", h, width = widths[i]);
        }
    }
    println!();
    for row in &table_rows {
        for (i, cell) in row.iter().enumerate() {
            if i > 0 { print!("{}", sep); }
            if i == headers.len() - 1 {
                print!("{}", cell);
            } else if (1..=4).contains(&i) {
                // Right-align numeric columns for easier comparison
                print!("{:>width$}", cell, width = widths[i]);
            } else {
                print!("{:<width$}", cell, width = widths[i]);
            }
        }
        println!();
    }

    Ok(())
}

fn format_age(duration: chrono::Duration) -> String {
    let secs = duration.num_seconds();
    if secs < 60 {
        format!("{}s ago", secs)
    } else if secs < 3600 {
        format!("{}m ago", secs / 60)
    } else if secs < 86400 {
        format!("{}h ago", secs / 3600)
    } else {
        format!("{}d ago", secs / 86400)
    }
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

    /// Fetch dispatchable allocatable totals for nodes matching a specific DB
    /// flavor value.
    ///
    /// The CPU total is computed by flooring each node's `cpu_millicores` to
    /// whole cores before summing. This reflects the bin-packing constraint:
    /// fractional leftover per node (e.g., 0.633 cores) cannot be consumed by
    /// whole-core jobs, so the safe "dispatchable" total is less than the raw
    /// SUM of effective allocatable. Memory and GPU are summed as-is.
    ///
    /// Falls back to cluster-wide totals if no nodes match the given flavor.
    pub async fn from_db_by_flavor(client: &Client, flavor: &str) -> Self {
        match client
            .query_one(
                "SELECT COALESCE(SUM((cpu_millicores / 1000) * 1000), 0)::BIGINT, \
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
