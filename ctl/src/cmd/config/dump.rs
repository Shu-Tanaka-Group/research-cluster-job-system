use anyhow::{Context, Result};
use k8s_openapi::api::core::v1::ConfigMap;
use kube::Api;
use std::collections::BTreeMap;

pub async fn run(k8s_client: &kube::Client, namespace: &str) -> Result<()> {
    let cms: Api<ConfigMap> = Api::namespaced(k8s_client.clone(), namespace);
    let cm = cms.get("cjob-config").await
        .context("Failed to get ConfigMap 'cjob-config'")?;

    // Build clean YAML structure
    let mut clean: BTreeMap<String, serde_yaml::Value> = BTreeMap::new();

    clean.insert(
        "apiVersion".to_string(),
        serde_yaml::Value::String("v1".to_string()),
    );
    clean.insert(
        "kind".to_string(),
        serde_yaml::Value::String("ConfigMap".to_string()),
    );

    // Clean metadata: keep only name and namespace
    let mut metadata: BTreeMap<String, serde_yaml::Value> = BTreeMap::new();
    if let Some(ref meta) = cm.metadata.name {
        metadata.insert(
            "name".to_string(),
            serde_yaml::Value::String(meta.clone()),
        );
    }
    if let Some(ref ns) = cm.metadata.namespace {
        metadata.insert(
            "namespace".to_string(),
            serde_yaml::Value::String(ns.clone()),
        );
    }
    clean.insert(
        "metadata".to_string(),
        serde_yaml::to_value(&metadata)?,
    );

    // Data: sort keys for consistent output
    if let Some(data) = cm.data {
        let sorted: BTreeMap<&String, &String> = data.iter().collect();
        clean.insert(
            "data".to_string(),
            serde_yaml::to_value(&sorted)?,
        );
    }

    let yaml = serde_yaml::to_string(&clean)
        .context("Failed to serialize ConfigMap to YAML")?;
    print!("{}", yaml);

    Ok(())
}
