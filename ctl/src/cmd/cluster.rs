use anyhow::Result;
use tokio_postgres::Client;

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
