use anyhow::Result;
use k8s_openapi::api::core::v1::ConfigMap;
use kube::Api;

pub async fn run(k8s_client: &kube::Client, namespace: &str) -> Result<()> {
    let cms: Api<ConfigMap> = Api::namespaced(k8s_client.clone(), namespace);
    let cm = cms.get("cjob-config").await?;

    let data = cm.data.unwrap_or_default();
    if data.is_empty() {
        println!("ConfigMap 'cjob-config' is empty.");
        return Ok(());
    }

    // Sort keys for consistent output
    let mut keys: Vec<&String> = data.keys().collect();
    keys.sort();

    println!("ConfigMap: cjob-config (namespace: {})", namespace);
    println!("{}", "─".repeat(60));
    for key in keys {
        println!("  {}: {}", key, data[key]);
    }
    Ok(())
}

/// Parse cluster totals from ConfigMap data for DRF calculation.
pub fn parse_cluster_totals(
    k8s_client: &kube::Client,
    namespace: &str,
) -> ClusterTotalsFetcher {
    ClusterTotalsFetcher {
        client: k8s_client.clone(),
        namespace: namespace.to_string(),
    }
}

pub struct ClusterTotalsFetcher {
    client: kube::Client,
    namespace: String,
}

impl ClusterTotalsFetcher {
    pub async fn fetch(&self) -> crate::cmd::usage::ClusterTotals {
        let cms: Api<ConfigMap> = Api::namespaced(self.client.clone(), &self.namespace);
        match cms.get("cjob-config").await {
            Ok(cm) => {
                let data = cm.data.unwrap_or_default();
                let cpu = data
                    .get("CLUSTER_TOTAL_CPU_MILLICORES")
                    .and_then(|v| v.parse().ok())
                    .unwrap_or(256000);
                let mem = data
                    .get("CLUSTER_TOTAL_MEMORY_MIB")
                    .and_then(|v| v.parse().ok())
                    .unwrap_or(1024000);
                let gpus = data
                    .get("CLUSTER_TOTAL_GPUS")
                    .and_then(|v| v.parse().ok())
                    .unwrap_or(0);
                crate::cmd::usage::ClusterTotals {
                    cpu_millicores: cpu,
                    memory_mib: mem,
                    gpus,
                }
            }
            Err(e) => {
                eprintln!(
                    "Warning: Could not fetch cjob-config ConfigMap ({}). Using defaults.",
                    e
                );
                crate::cmd::usage::ClusterTotals::default()
            }
        }
    }
}
