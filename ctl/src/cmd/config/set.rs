use anyhow::{bail, Context, Result};
use k8s_openapi::api::core::v1::ConfigMap;
use kube::api::{Api, Patch, PatchParams};
use std::io::{self, Write};

#[derive(Clone, Copy)]
enum ValueType {
    String,
    Integer,
    Float,
    Boolean,
    Json,
}

struct ConfigKeyMeta {
    key: &'static str,
    value_type: ValueType,
    components: &'static [&'static str],
    updatable: bool,
}

const CONFIG_KEYS: &[ConfigKeyMeta] = &[
    // DB connection (not updatable)
    ConfigKeyMeta { key: "POSTGRES_HOST", value_type: ValueType::String, components: &["dispatcher", "watcher", "submit-api"], updatable: false },
    ConfigKeyMeta { key: "POSTGRES_PORT", value_type: ValueType::Integer, components: &["dispatcher", "watcher", "submit-api"], updatable: false },
    ConfigKeyMeta { key: "POSTGRES_DB", value_type: ValueType::String, components: &["dispatcher", "watcher", "submit-api"], updatable: false },
    ConfigKeyMeta { key: "POSTGRES_USER", value_type: ValueType::String, components: &["dispatcher", "watcher", "submit-api"], updatable: false },
    ConfigKeyMeta { key: "POSTGRES_PASSWORD", value_type: ValueType::String, components: &["dispatcher", "watcher", "submit-api"], updatable: false },
    // Dispatcher
    ConfigKeyMeta { key: "DISPATCH_BUDGET_PER_NAMESPACE", value_type: ValueType::Integer, components: &["dispatcher"], updatable: true },
    ConfigKeyMeta { key: "DISPATCH_BATCH_SIZE", value_type: ValueType::Integer, components: &["dispatcher"], updatable: true },
    ConfigKeyMeta { key: "DISPATCH_FETCH_MULTIPLIER", value_type: ValueType::Integer, components: &["dispatcher"], updatable: true },
    ConfigKeyMeta { key: "DISPATCH_ROUND_SIZE", value_type: ValueType::Integer, components: &["dispatcher"], updatable: true },
    ConfigKeyMeta { key: "DISPATCH_BUDGET_CHECK_INTERVAL_SEC", value_type: ValueType::Integer, components: &["dispatcher", "watcher"], updatable: true },
    ConfigKeyMeta { key: "DISPATCH_RETRY_INTERVAL_SEC", value_type: ValueType::Integer, components: &["dispatcher"], updatable: true },
    ConfigKeyMeta { key: "DISPATCH_MAX_RETRIES", value_type: ValueType::Integer, components: &["dispatcher"], updatable: true },
    ConfigKeyMeta { key: "GAP_FILLING_ENABLED", value_type: ValueType::Boolean, components: &["dispatcher"], updatable: true },
    ConfigKeyMeta { key: "GAP_FILLING_STALL_THRESHOLD_SEC", value_type: ValueType::Integer, components: &["dispatcher"], updatable: true },
    ConfigKeyMeta { key: "FAIR_SHARE_WINDOW_DAYS", value_type: ValueType::Integer, components: &["dispatcher", "submit-api"], updatable: true },
    ConfigKeyMeta { key: "CPU_LIMIT_BUFFER_MULTIPLIER", value_type: ValueType::Float, components: &["dispatcher"], updatable: true },
    // ResourceFlavor
    ConfigKeyMeta { key: "RESOURCE_FLAVORS", value_type: ValueType::Json, components: &["dispatcher", "watcher", "submit-api"], updatable: true },
    ConfigKeyMeta { key: "DEFAULT_FLAVOR", value_type: ValueType::String, components: &["submit-api"], updatable: true },
    ConfigKeyMeta { key: "NODE_RESOURCE_SYNC_INTERVAL_SEC", value_type: ValueType::Integer, components: &["watcher"], updatable: true },
    // Watcher
    ConfigKeyMeta { key: "CLUSTER_QUEUE_NAME", value_type: ValueType::String, components: &["watcher"], updatable: true },
    ConfigKeyMeta { key: "RESOURCE_QUOTA_NAME", value_type: ValueType::String, components: &["watcher"], updatable: true },
    ConfigKeyMeta { key: "RESOURCE_QUOTA_SYNC_INTERVAL_SEC", value_type: ValueType::Integer, components: &["watcher"], updatable: true },
    ConfigKeyMeta { key: "WATCHER_METRICS_PORT", value_type: ValueType::Integer, components: &["watcher"], updatable: true },
    // Submit API
    ConfigKeyMeta { key: "MAX_QUEUED_JOBS_PER_NAMESPACE", value_type: ValueType::Integer, components: &["submit-api"], updatable: true },
    ConfigKeyMeta { key: "MAX_SWEEP_COMPLETIONS", value_type: ValueType::Integer, components: &["submit-api"], updatable: true },
    ConfigKeyMeta { key: "DEFAULT_TIME_LIMIT_SECONDS", value_type: ValueType::Integer, components: &["submit-api"], updatable: true },
    ConfigKeyMeta { key: "MAX_TIME_LIMIT_SECONDS", value_type: ValueType::Integer, components: &["submit-api"], updatable: true },
    ConfigKeyMeta { key: "CLI_BINARY_DIR", value_type: ValueType::String, components: &["submit-api"], updatable: true },
    // K8s / Kueue
    ConfigKeyMeta { key: "KUEUE_LOCAL_QUEUE_NAME", value_type: ValueType::String, components: &["dispatcher"], updatable: true },
    ConfigKeyMeta { key: "USER_NAMESPACE_LABEL", value_type: ValueType::String, components: &["watcher"], updatable: true },
    ConfigKeyMeta { key: "TTL_SECONDS_AFTER_FINISHED", value_type: ValueType::Integer, components: &["dispatcher"], updatable: true },
    ConfigKeyMeta { key: "JOB_NODE_TAINT", value_type: ValueType::String, components: &["dispatcher"], updatable: true },
    // Paths
    ConfigKeyMeta { key: "WORKSPACE_MOUNT_PATH", value_type: ValueType::String, components: &["dispatcher"], updatable: true },
    ConfigKeyMeta { key: "LOG_BASE_DIR", value_type: ValueType::String, components: &["submit-api"], updatable: true },
    // Logging
    ConfigKeyMeta { key: "LOG_LEVEL", value_type: ValueType::String, components: &["dispatcher", "watcher", "submit-api"], updatable: true },
];

fn find_key(name: &str) -> Option<&'static ConfigKeyMeta> {
    CONFIG_KEYS.iter().find(|m| m.key == name)
}

fn validate_value(meta: &ConfigKeyMeta, value: &str) -> Result<String> {
    match meta.value_type {
        ValueType::String => Ok(value.to_string()),
        ValueType::Integer => {
            value
                .parse::<i64>()
                .with_context(|| format!("'{}' expects an integer value, got '{}'", meta.key, value))?;
            Ok(value.to_string())
        }
        ValueType::Float => {
            value
                .parse::<f64>()
                .with_context(|| format!("'{}' expects a numeric value, got '{}'", meta.key, value))?;
            Ok(value.to_string())
        }
        ValueType::Boolean => {
            match value.to_lowercase().as_str() {
                "true" | "false" => Ok(value.to_lowercase()),
                _ => bail!("'{}' expects 'true' or 'false', got '{}'", meta.key, value),
            }
        }
        ValueType::Json => {
            serde_json::from_str::<serde_json::Value>(value)
                .with_context(|| format!("'{}' expects valid JSON, got invalid input", meta.key))?;
            Ok(value.to_string())
        }
    }
}

fn truncate_display(s: &str, max_len: usize) -> String {
    let single_line = s.replace('\n', "\\n");
    if single_line.len() > max_len {
        format!("{}...", &single_line[..max_len])
    } else {
        single_line
    }
}

pub async fn run(
    k8s_client: &kube::Client,
    namespace: &str,
    key: &str,
    value: Option<&str>,
    from_file: Option<&str>,
    skip_confirm: bool,
) -> Result<()> {
    // Resolve value
    let raw_value = match (value, from_file) {
        (Some(v), None) => v.to_string(),
        (None, Some(path)) => {
            std::fs::read_to_string(path)
                .with_context(|| format!("Failed to read file '{}'", path))?
                .trim_end()
                .to_string()
        }
        (None, None) => bail!("Provide VALUE or --from-file <path>"),
        _ => unreachable!(), // conflicts_with prevents this
    };

    // Look up key metadata
    let meta = find_key(key)
        .ok_or_else(|| anyhow::anyhow!(
            "Unknown config key '{}'. Use 'cjobctl config show' to see valid keys.",
            key
        ))?;

    if !meta.updatable {
        bail!(
            "Key '{}' cannot be updated via this command (requires infrastructure change).",
            key
        );
    }

    // Validate value
    let validated_value = validate_value(meta, &raw_value)?;

    // Fetch current ConfigMap to show old value
    let cms: Api<ConfigMap> = Api::namespaced(k8s_client.clone(), namespace);
    let cm = cms.get("cjob-config").await
        .context("Failed to get ConfigMap 'cjob-config'")?;
    let data = cm.data.unwrap_or_default();
    let old_value = data.get(key).map(|s| s.as_str()).unwrap_or("<not set>");

    // Show change and confirm
    println!(
        "{}: {} \u{2192} {}",
        key,
        truncate_display(old_value, 60),
        truncate_display(&validated_value, 60),
    );

    if !skip_confirm {
        print!("Proceed? [y/N] ");
        io::stdout().flush()?;
        let mut input = String::new();
        io::stdin().read_line(&mut input)?;
        if !input.trim().eq_ignore_ascii_case("y") {
            println!("Aborted.");
            return Ok(());
        }
    }

    // Apply patch
    let patch = serde_json::json!({ "data": { key: &validated_value } });
    cms.patch("cjob-config", &PatchParams::default(), &Patch::Merge(&patch))
        .await
        .with_context(|| format!("Failed to update key '{}' in ConfigMap", key))?;

    println!("\nUpdated '{}' in cjob-config.", key);

    // Show restart guidance
    println!("\nRestart the following component(s) to apply:");
    for comp in meta.components {
        println!("  cjobctl system restart {}", comp);
    }

    Ok(())
}
