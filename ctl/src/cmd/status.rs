use anyhow::Result;
use k8s_openapi::api::core::v1::Pod;
use kube::api::ListParams;
use kube::Api;

pub async fn run(k8s_client: &kube::Client, namespace: &str) -> Result<()> {
    let pods: Api<Pod> = Api::namespaced(k8s_client.clone(), namespace);
    let pod_list = pods.list(&ListParams::default()).await?;

    if pod_list.items.is_empty() {
        println!("No pods found in namespace '{}'.", namespace);
        return Ok(());
    }

    println!(
        "{:<40} {:<12} {:<10} {}",
        "NAME", "STATUS", "RESTARTS", "AGE"
    );
    for pod in &pod_list.items {
        let name = pod
            .metadata
            .name
            .as_deref()
            .unwrap_or("<unknown>");

        let phase = pod
            .status
            .as_ref()
            .and_then(|s| s.phase.as_deref())
            .unwrap_or("Unknown");

        let restarts: i32 = pod
            .status
            .as_ref()
            .and_then(|s| s.container_statuses.as_ref())
            .map(|cs| cs.iter().map(|c| c.restart_count).sum())
            .unwrap_or(0);

        let age = pod
            .metadata
            .creation_timestamp
            .as_ref()
            .map(|ts| {
                let elapsed = chrono::Utc::now()
                    .signed_duration_since(ts.0)
                    .num_seconds()
                    .max(0);
                format_age(elapsed as u64)
            })
            .unwrap_or_else(|| "-".to_string());

        println!("{:<40} {:<12} {:<10} {}", name, phase, restarts, age);
    }
    Ok(())
}

fn format_age(seconds: u64) -> String {
    let days = seconds / 86400;
    let hours = (seconds % 86400) / 3600;
    let minutes = (seconds % 3600) / 60;
    if days > 0 {
        format!("{}d{}h", days, hours)
    } else if hours > 0 {
        format!("{}h{}m", hours, minutes)
    } else {
        format!("{}m", minutes)
    }
}
