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
