use anyhow::{bail, Context, Result};
use kube::api::{Api, ApiResource, DynamicObject, GroupVersionKind, PostParams};
use serde_json::Value;
use std::io::{self, Write};
use tokio_postgres::Client;

use super::usage::ClusterTotals;

const CLUSTER_QUEUE_NAME: &str = "cjob-cluster-queue";

pub async fn resources(client: &Client) -> Result<()> {
    let rows = client
        .query(
            "SELECT node_name, cpu_millicores, memory_mib, gpu, updated_at \
             FROM node_resources ORDER BY node_name",
            &[],
        )
        .await?;

    if rows.is_empty() {
        println!("No node resource data found. Is the Watcher running?");
        return Ok(());
    }

    println!("=== Node Resources ===");
    println!(
        "{:<24} {:>12} {:>14} {:>6} {:>22}",
        "NODE", "CPU (cores)", "Memory (GiB)", "GPU", "Updated"
    );
    for row in &rows {
        let name: &str = row.get(0);
        let cpu: i32 = row.get(1);
        let mem: i32 = row.get(2);
        let gpu: i32 = row.get(3);
        let updated: chrono::DateTime<chrono::Utc> = row.get(4);
        println!(
            "{:<24} {:>12} {:>14.1} {:>6} {:>22}",
            name,
            cpu as f64 / 1000.0,
            mem as f64 / 1024.0,
            gpu,
            updated.format("%Y-%m-%d %H:%M:%S"),
        );
    }

    // Cluster totals
    let totals = client
        .query_one(
            "SELECT COALESCE(SUM(cpu_millicores), 0)::BIGINT, \
                    COALESCE(SUM(memory_mib), 0)::BIGINT, \
                    COALESCE(SUM(gpu), 0)::BIGINT \
             FROM node_resources",
            &[],
        )
        .await?;
    let total_cpu: i64 = totals.get(0);
    let total_mem: i64 = totals.get(1);
    let total_gpu: i64 = totals.get(2);

    println!();
    println!("=== Cluster Totals (for DRF normalization) ===");
    println!("CPU:    {} cores ({}m)", total_cpu / 1000, total_cpu);
    println!("Memory: {:.1} GiB ({} MiB)", total_mem as f64 / 1024.0, total_mem);
    println!("GPU:    {}", total_gpu);

    // Max per node
    let maxes = client
        .query_one(
            "SELECT MAX(cpu_millicores), MAX(memory_mib), MAX(gpu) \
             FROM node_resources",
            &[],
        )
        .await?;
    let max_cpu: Option<i32> = maxes.get(0);
    let max_mem: Option<i32> = maxes.get(1);
    let max_gpu: Option<i32> = maxes.get(2);

    if let (Some(mc), Some(mm), Some(mg)) = (max_cpu, max_mem, max_gpu) {
        println!();
        println!("=== Max per Node (Submit API rejection threshold) ===");
        println!("CPU:    {} cores ({}m)", mc / 1000, mc);
        println!("Memory: {:.1} GiB ({} MiB)", mm as f64 / 1024.0, mm);
        println!("GPU:    {}", mg);
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// show-quota
// ---------------------------------------------------------------------------

fn cluster_queue_api(k8s_client: &kube::Client) -> Api<DynamicObject> {
    let gvk = GroupVersionKind::gvk("kueue.x-k8s.io", "v1beta2", "ClusterQueue");
    let ar = ApiResource::from_gvk(&gvk);
    Api::all_with(k8s_client.clone(), &ar)
}

struct QuotaValues {
    cpu: Option<String>,
    memory: Option<String>,
    gpu: Option<String>,
}

fn extract_quota(cq: &DynamicObject) -> QuotaValues {
    let mut vals = QuotaValues {
        cpu: None,
        memory: None,
        gpu: None,
    };

    if let Some(groups) = cq.data["spec"]["resourceGroups"].as_array() {
        for group in groups {
            if let Some(flavors) = group["flavors"].as_array() {
                for flavor in flavors {
                    if let Some(resources) = flavor["resources"].as_array() {
                        for res in resources {
                            let name = res["name"].as_str().unwrap_or("");
                            let quota = res["nominalQuota"].as_str().unwrap_or("").to_string();
                            match name {
                                "cpu" => vals.cpu = Some(quota),
                                "memory" => vals.memory = Some(quota),
                                "nvidia.com/gpu" => vals.gpu = Some(quota),
                                _ => {}
                            }
                        }
                    }
                }
            }
        }
    }

    vals
}

pub async fn show_quota(k8s_client: &kube::Client) -> Result<()> {
    let api = cluster_queue_api(k8s_client);
    let cq = api
        .get(CLUSTER_QUEUE_NAME)
        .await
        .context("Failed to get ClusterQueue")?;

    let vals = extract_quota(&cq);

    println!("=== ClusterQueue nominalQuota ({}) ===", CLUSTER_QUEUE_NAME);
    println!(
        "CPU:    {}",
        vals.cpu.as_deref().unwrap_or("(not set)")
    );
    println!(
        "Memory: {}",
        vals.memory.as_deref().unwrap_or("(not set)")
    );
    println!(
        "GPU:    {}",
        vals.gpu.as_deref().unwrap_or("(not set)")
    );

    Ok(())
}

// ---------------------------------------------------------------------------
// set-quota
// ---------------------------------------------------------------------------

fn validate_memory_format(s: &str) -> Result<()> {
    // Accept Kubernetes quantity formats: digits followed by Ki/Mi/Gi/Ti
    let suffixes = ["Ti", "Gi", "Mi", "Ki"];
    for suffix in &suffixes {
        if let Some(num_part) = s.strip_suffix(suffix) {
            if num_part.parse::<u64>().is_ok() {
                return Ok(());
            }
        }
    }
    // Also accept plain integer (bytes) — unlikely but valid in K8s
    if s.parse::<u64>().is_ok() {
        return Ok(());
    }
    bail!(
        "Invalid memory format: '{}'. Use a Kubernetes quantity like '1000Gi', '512Mi', etc.",
        s
    );
}

/// Convert a memory quantity string to MiB for comparison.
fn memory_to_mib(s: &str) -> Option<u64> {
    if let Some(n) = s.strip_suffix("Ti") {
        n.parse::<u64>().ok().map(|v| v * 1024 * 1024)
    } else if let Some(n) = s.strip_suffix("Gi") {
        n.parse::<u64>().ok().map(|v| v * 1024)
    } else if let Some(n) = s.strip_suffix("Mi") {
        n.parse::<u64>().ok()
    } else if let Some(n) = s.strip_suffix("Ki") {
        n.parse::<u64>().ok().map(|v| v / 1024)
    } else {
        // plain bytes → MiB
        s.parse::<u64>().ok().map(|v| v / (1024 * 1024))
    }
}

fn confirm(prompt: &str) -> bool {
    print!("{} [y/N]: ", prompt);
    io::stdout().flush().ok();
    let mut input = String::new();
    if io::stdin().read_line(&mut input).is_err() {
        return false;
    }
    matches!(input.trim(), "y" | "Y" | "yes" | "Yes")
}

pub async fn set_quota(
    db_client: &Client,
    k8s_client: &kube::Client,
    cpu: Option<u32>,
    memory: Option<&str>,
    gpu: Option<u32>,
    force: bool,
) -> Result<()> {
    if cpu.is_none() && memory.is_none() && gpu.is_none() {
        bail!("Specify at least one of --cpu, --memory, or --gpu.");
    }

    // Validate memory format
    if let Some(mem) = memory {
        validate_memory_format(mem)?;
    }

    // Fetch allocatable totals from DB for validation
    let totals = ClusterTotals::from_db(db_client).await;
    let alloc_cpu_cores = totals.cpu_millicores / 1000;
    let alloc_mem_mib = totals.memory_mib;
    let alloc_gpu = totals.gpus;

    let mut exceeds = false;

    // Validate CPU
    if let Some(c) = cpu {
        if (c as i64) > alloc_cpu_cores {
            exceeds = true;
            eprintln!(
                "Error: CPU {} exceeds cluster allocatable total ({} cores)",
                c, alloc_cpu_cores,
            );
        } else if (c as i64) < alloc_cpu_cores / 10 {
            eprintln!(
                "Warning: CPU {} is very small compared to cluster allocatable total ({} cores)",
                c, alloc_cpu_cores,
            );
        }
    }

    // Validate memory
    if let Some(mem) = memory {
        if let Some(mem_mib) = memory_to_mib(mem) {
            if (mem_mib as i64) > alloc_mem_mib {
                exceeds = true;
                eprintln!(
                    "Error: Memory {} exceeds cluster allocatable total ({:.1} GiB)",
                    mem,
                    alloc_mem_mib as f64 / 1024.0,
                );
            } else if (mem_mib as i64) < alloc_mem_mib / 10 {
                eprintln!(
                    "Warning: Memory {} is very small compared to cluster allocatable total ({:.1} GiB)",
                    mem,
                    alloc_mem_mib as f64 / 1024.0,
                );
            }
        }
    }

    // Validate GPU
    if let Some(g) = gpu {
        if (g as i64) > alloc_gpu {
            exceeds = true;
            eprintln!(
                "Error: GPU {} exceeds cluster allocatable total ({})",
                g, alloc_gpu,
            );
        } else if alloc_gpu > 0 && (g as i64) < alloc_gpu / 10 {
            eprintln!(
                "Warning: GPU {} is very small compared to cluster allocatable total ({})",
                g, alloc_gpu,
            );
        }
    }

    if exceeds && !force {
        bail!("Specified values exceed cluster allocatable totals. Use --force to override.");
    }

    // Fetch current ClusterQueue
    let api = cluster_queue_api(k8s_client);
    let cq = api
        .get(CLUSTER_QUEUE_NAME)
        .await
        .context("Failed to get ClusterQueue")?;

    let current = extract_quota(&cq);

    // Show current → new (only for specified resources)
    println!("=== ClusterQueue nominalQuota change ===");
    match cpu {
        Some(c) => println!(
            "CPU:    {} → {}",
            current.cpu.as_deref().unwrap_or("(not set)"),
            c,
        ),
        None => println!(
            "CPU:    {} (unchanged)",
            current.cpu.as_deref().unwrap_or("(not set)"),
        ),
    }
    match memory {
        Some(m) => println!(
            "Memory: {} → {}",
            current.memory.as_deref().unwrap_or("(not set)"),
            m,
        ),
        None => println!(
            "Memory: {} (unchanged)",
            current.memory.as_deref().unwrap_or("(not set)"),
        ),
    }
    match gpu {
        Some(g) => println!(
            "GPU:    {} → {}",
            current.gpu.as_deref().unwrap_or("(not set)"),
            g,
        ),
        None => {
            if let Some(ref cur_gpu) = current.gpu {
                println!("GPU:    {} (unchanged)", cur_gpu);
            }
        }
    }

    if !confirm("Apply this change?") {
        println!("Aborted.");
        return Ok(());
    }

    // Build updated ClusterQueue
    let mut cq = cq;
    let spec = cq
        .data
        .as_object_mut()
        .and_then(|d| d.get_mut("spec"))
        .context("ClusterQueue has no spec")?;

    let groups = spec["resourceGroups"]
        .as_array_mut()
        .context("ClusterQueue has no resourceGroups")?;

    // We expect a single resourceGroup with a single flavor
    let flavor_resources = groups
        .get_mut(0)
        .and_then(|g| g["flavors"].as_array_mut())
        .and_then(|f| f.get_mut(0))
        .and_then(|f| f["resources"].as_array_mut())
        .context("Unexpected ClusterQueue structure")?;

    // Update only specified resources
    for res in flavor_resources.iter_mut() {
        match res["name"].as_str() {
            Some("cpu") => {
                if let Some(c) = cpu {
                    res["nominalQuota"] = Value::String(c.to_string());
                }
            }
            Some("memory") => {
                if let Some(m) = memory {
                    res["nominalQuota"] = Value::String(m.to_string());
                }
            }
            Some("nvidia.com/gpu") => {
                if let Some(g) = gpu {
                    if g == 0 {
                        // Mark for removal (handled below)
                    } else {
                        res["nominalQuota"] = Value::String(g.to_string());
                    }
                }
            }
            _ => {}
        }
    }

    // Handle GPU addition/removal
    if let Some(g) = gpu {
        let has_gpu = flavor_resources
            .iter()
            .any(|r| r["name"].as_str() == Some("nvidia.com/gpu"));

        if g == 0 {
            // Remove GPU entry
            flavor_resources.retain(|r| r["name"].as_str() != Some("nvidia.com/gpu"));

            // Also remove from coveredResources
            if let Some(covered) = groups
                .get_mut(0)
                .and_then(|g| g["coveredResources"].as_array_mut())
            {
                covered.retain(|v| v.as_str() != Some("nvidia.com/gpu"));
            }
        } else if !has_gpu {
            // Add GPU entry
            flavor_resources.push(serde_json::json!({
                "name": "nvidia.com/gpu",
                "nominalQuota": g.to_string(),
            }));

            // Add to coveredResources
            if let Some(covered) = groups
                .get_mut(0)
                .and_then(|g| g["coveredResources"].as_array_mut())
            {
                if !covered.iter().any(|v| v.as_str() == Some("nvidia.com/gpu")) {
                    covered.push(Value::String("nvidia.com/gpu".to_string()));
                }
            }
        }
    }

    // Apply update
    let pp = PostParams::default();
    api.replace(CLUSTER_QUEUE_NAME, &pp, &cq)
        .await
        .context("Failed to update ClusterQueue")?;

    println!("ClusterQueue '{}' updated successfully.", CLUSTER_QUEUE_NAME);

    Ok(())
}
