use anyhow::{bail, Context, Result};
use k8s_openapi::api::core::v1::ConfigMap;
use kube::api::{Api, Patch, PatchParams};
use std::collections::BTreeMap;
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
    ConfigKeyMeta { key: "USAGE_RETENTION_DAYS", value_type: ValueType::Integer, components: &["dispatcher"], updatable: true },
    ConfigKeyMeta { key: "CPU_LIMIT_BUFFER_MULTIPLIER", value_type: ValueType::Float, components: &["dispatcher"], updatable: true },
    // ResourceFlavor
    ConfigKeyMeta { key: "RESOURCE_FLAVORS", value_type: ValueType::Json, components: &["dispatcher", "watcher", "submit-api"], updatable: true },
    ConfigKeyMeta { key: "DEFAULT_FLAVOR", value_type: ValueType::String, components: &["submit-api"], updatable: true },
    ConfigKeyMeta { key: "NODE_RESOURCE_SYNC_INTERVAL_SEC", value_type: ValueType::Integer, components: &["watcher"], updatable: true },
    // Watcher
    ConfigKeyMeta { key: "WATCHER_K8S_LIST_PAGE_SIZE", value_type: ValueType::Integer, components: &["watcher"], updatable: true },
    ConfigKeyMeta { key: "WATCHER_DISPATCH_GRACE_SEC", value_type: ValueType::Integer, components: &["watcher"], updatable: true },
    ConfigKeyMeta { key: "WATCHER_DISPATCH_TIMEOUT_SEC", value_type: ValueType::Integer, components: &["watcher"], updatable: true },
    ConfigKeyMeta { key: "WATCHER_DISPATCH_BACKOFF_MAX_SEC", value_type: ValueType::Integer, components: &["watcher"], updatable: true },
    ConfigKeyMeta { key: "CLUSTER_QUEUE_NAME", value_type: ValueType::String, components: &["watcher"], updatable: true },
    ConfigKeyMeta { key: "RESOURCE_QUOTA_NAME", value_type: ValueType::String, components: &["watcher"], updatable: true },
    ConfigKeyMeta { key: "RESOURCE_QUOTA_SYNC_INTERVAL_SEC", value_type: ValueType::Integer, components: &["watcher"], updatable: true },
    ConfigKeyMeta { key: "WATCHER_METRICS_PORT", value_type: ValueType::Integer, components: &["watcher"], updatable: true },
    ConfigKeyMeta { key: "DISPATCHER_METRICS_PORT", value_type: ValueType::Integer, components: &["dispatcher"], updatable: true },
    // Submit API
    ConfigKeyMeta { key: "MAX_QUEUED_JOBS_PER_NAMESPACE", value_type: ValueType::Integer, components: &["submit-api"], updatable: true },
    ConfigKeyMeta { key: "MAX_SWEEP_COMPLETIONS", value_type: ValueType::Integer, components: &["submit-api"], updatable: true },
    ConfigKeyMeta { key: "DEFAULT_TIME_LIMIT_SECONDS", value_type: ValueType::Integer, components: &["submit-api"], updatable: true },
    ConfigKeyMeta { key: "MAX_TIME_LIMIT_SECONDS", value_type: ValueType::Integer, components: &["submit-api"], updatable: true },
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

/// Fields allowed in a `RESOURCE_FLAVORS` entry.
///
/// This list is the CLI-side mirror of the schema in
/// `docs/architecture/resources.md`; keep both in sync with the server-side
/// `FlavorDefinition` (which sets `extra="forbid"`).
const FLAVOR_FIELDS: &[&str] = &["name", "label_selector", "gpu_resource_name", "image"];

/// Structurally validate a `RESOURCE_FLAVORS` value and return the flavor
/// names in definition order.
///
/// All violations are collected and reported together rather than failing on
/// the first one, so an administrator can fix the whole file in one pass.
fn validate_resource_flavors(value: &str) -> Result<Vec<String>> {
    let parsed: serde_json::Value = serde_json::from_str(value)
        .context("'RESOURCE_FLAVORS' expects valid JSON, got invalid input")?;

    let items = parsed.as_array().ok_or_else(|| {
        anyhow::anyhow!("'RESOURCE_FLAVORS' must be a JSON array of flavor definitions")
    })?;

    if items.is_empty() {
        bail!("'RESOURCE_FLAVORS' must define at least one flavor");
    }

    let mut errors: Vec<String> = Vec::new();
    let mut names: Vec<String> = Vec::new();

    for (i, item) in items.iter().enumerate() {
        let Some(obj) = item.as_object() else {
            errors.push(format!("flavors[{}]: must be a JSON object", i));
            continue;
        };

        for field in ["name", "label_selector"] {
            match obj.get(field) {
                None => errors.push(format!("flavors[{}]: missing required field '{}'", i, field)),
                Some(v) => match v.as_str() {
                    None => errors.push(format!("flavors[{}]: '{}' must be a string", i, field)),
                    Some("") => errors.push(format!("flavors[{}]: '{}' must not be empty", i, field)),
                    Some(_) => {}
                },
            }
        }

        for field in ["gpu_resource_name", "image"] {
            match obj.get(field) {
                None => {}
                Some(v) if v.is_null() => {}
                Some(v) => match v.as_str() {
                    None => errors.push(format!("flavors[{}]: '{}' must be a string", i, field)),
                    Some("") => {
                        errors.push(format!("flavors[{}]: '{}' must not be empty", i, field))
                    }
                    Some(_) => {}
                },
            }
        }

        for key in obj.keys() {
            if !FLAVOR_FIELDS.contains(&key.as_str()) {
                errors.push(format!(
                    "flavors[{}]: unknown field '{}' (allowed: {})",
                    i,
                    key,
                    FLAVOR_FIELDS.join(", ")
                ));
            }
        }

        if let Some(sel) = obj.get("label_selector").and_then(|v| v.as_str()) {
            if !sel.is_empty() {
                let parts: Vec<&str> = sel.split('=').collect();
                if parts.len() != 2 || parts[0].is_empty() || parts[1].is_empty() {
                    errors.push(format!(
                        "flavors[{}]: 'label_selector' must be in 'key=value' form, got '{}'",
                        i, sel
                    ));
                }
            }
        }

        if let Some(name) = obj.get("name").and_then(|v| v.as_str()) {
            if !name.is_empty() {
                if names.iter().any(|n| n == name) {
                    errors.push(format!("flavors[{}]: duplicate 'name' value '{}'", i, name));
                } else {
                    names.push(name.to_string());
                }
            }
        }
    }

    if !errors.is_empty() {
        bail!(
            "'RESOURCE_FLAVORS' has invalid flavor definitions:\n  - {}",
            errors.join("\n  - ")
        );
    }

    Ok(names)
}

/// Validate a value against the rest of the ConfigMap.
///
/// `RESOURCE_FLAVORS` and `DEFAULT_FLAVOR` must stay consistent, so both keys
/// verify the post-update combination. When the counterpart key is missing or
/// unparsable the check is skipped with a warning instead of failing, so that a
/// broken `RESOURCE_FLAVORS` cannot block repairing `DEFAULT_FLAVOR` (and vice
/// versa). Warnings go to stderr; `w` is a parameter so tests can capture them.
fn validate_against_configmap(
    key: &str,
    value: &str,
    data: &BTreeMap<String, String>,
    w: &mut dyn Write,
) -> Result<()> {
    match key {
        "RESOURCE_FLAVORS" => {
            let names = validate_resource_flavors(value)?;
            match data.get("DEFAULT_FLAVOR") {
                None => {
                    writeln!(
                        w,
                        "Warning: 'DEFAULT_FLAVOR' is not set in the ConfigMap; skipping the consistency check."
                    )?;
                }
                Some(default) => {
                    let default = default.trim();
                    if !names.iter().any(|n| n == default) {
                        bail!(
                            "current 'DEFAULT_FLAVOR' value '{}' does not match any flavor name in the new RESOURCE_FLAVORS (available: {})",
                            default,
                            names.join(", ")
                        );
                    }
                }
            }
            Ok(())
        }
        "DEFAULT_FLAVOR" => {
            let Some(raw) = data.get("RESOURCE_FLAVORS") else {
                writeln!(
                    w,
                    "Warning: 'RESOURCE_FLAVORS' is not set in the ConfigMap; skipping the consistency check."
                )?;
                return Ok(());
            };
            let Ok(names) = validate_resource_flavors(raw) else {
                writeln!(
                    w,
                    "Warning: the current 'RESOURCE_FLAVORS' in the ConfigMap is not a valid flavor definition list; skipping the consistency check."
                )?;
                return Ok(());
            };
            let value = value.trim();
            if !names.iter().any(|n| n == value) {
                bail!(
                    "'DEFAULT_FLAVOR' value '{}' does not match any flavor name in RESOURCE_FLAVORS (available: {})",
                    value,
                    names.join(", ")
                );
            }
            Ok(())
        }
        _ => Ok(()),
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

    // Validate value type
    let validated_value = validate_value(meta, &raw_value)?;

    // Fetch current ConfigMap to show old value and to cross-check related keys
    let cms: Api<ConfigMap> = Api::namespaced(k8s_client.clone(), namespace);
    let cm = cms.get("cjob-config").await
        .context("Failed to get ConfigMap 'cjob-config'")?;
    let data = cm.data.unwrap_or_default();

    // Key-specific structural validation (needs the rest of the ConfigMap)
    validate_against_configmap(key, &validated_value, &data, &mut io::stderr())?;

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

#[cfg(test)]
mod tests {
    use super::*;

    const VALID: &str = r#"[
        {"name": "cpu", "label_selector": "cjob.io/flavor=cpu"},
        {"name": "gpu", "label_selector": "cjob.io/flavor=gpu", "gpu_resource_name": "nvidia.com/gpu"}
    ]"#;

    fn errors_of(value: &str) -> String {
        validate_resource_flavors(value).unwrap_err().to_string()
    }

    fn cm(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    /// Runs validate_against_configmap capturing warnings, returning
    /// (result, captured stderr).
    fn check(key: &str, value: &str, data: &BTreeMap<String, String>) -> (Result<()>, String) {
        let mut buf: Vec<u8> = Vec::new();
        let result = validate_against_configmap(key, value, data, &mut buf);
        (result, String::from_utf8(buf).unwrap())
    }

    // --- structural validation: happy path ---

    #[test]
    fn valid_flavors_return_names_in_order() {
        let names = validate_resource_flavors(VALID).unwrap();
        assert_eq!(names, vec!["cpu".to_string(), "gpu".to_string()]);
    }

    #[test]
    fn gpu_resource_name_may_be_omitted_or_null() {
        let value = r#"[
            {"name": "cpu", "label_selector": "k=cpu"},
            {"name": "gpu", "label_selector": "k=gpu", "gpu_resource_name": null}
        ]"#;
        assert_eq!(validate_resource_flavors(value).unwrap().len(), 2);
    }

    #[test]
    fn multiline_configmap_style_value_is_accepted() {
        // ConfigMap block scalars keep the trailing newline
        let value = format!("{}\n", VALID);
        assert!(validate_resource_flavors(&value).is_ok());
    }

    // --- structural validation: errors ---

    #[test]
    fn invalid_json_is_rejected() {
        let err = errors_of("[{");
        assert!(err.contains("expects valid JSON"), "{}", err);
    }

    #[test]
    fn top_level_must_be_an_array() {
        let err = errors_of(r#"{"name": "cpu", "label_selector": "k=cpu"}"#);
        assert!(err.contains("must be a JSON array"), "{}", err);
    }

    #[test]
    fn empty_array_is_rejected() {
        let err = errors_of("[]");
        assert!(err.contains("at least one flavor"), "{}", err);
    }

    #[test]
    fn element_must_be_an_object() {
        let err = errors_of(r#"["cpu"]"#);
        assert!(err.contains("flavors[0]: must be a JSON object"), "{}", err);
    }

    #[test]
    fn missing_required_fields_are_reported() {
        let err = errors_of(r#"[{"gpu_resource_name": "nvidia.com/gpu"}]"#);
        assert!(err.contains("missing required field 'name'"), "{}", err);
        assert!(
            err.contains("missing required field 'label_selector'"),
            "{}",
            err
        );
    }

    #[test]
    fn empty_required_fields_are_reported() {
        let err = errors_of(r#"[{"name": "", "label_selector": ""}]"#);
        assert!(err.contains("'name' must not be empty"), "{}", err);
        assert!(err.contains("'label_selector' must not be empty"), "{}", err);
    }

    #[test]
    fn non_string_required_fields_are_reported() {
        let err = errors_of(r#"[{"name": 1, "label_selector": ["k=v"]}]"#);
        assert!(err.contains("'name' must be a string"), "{}", err);
        assert!(err.contains("'label_selector' must be a string"), "{}", err);
    }

    #[test]
    fn unknown_field_is_rejected() {
        let err = errors_of(
            r#"[{"name": "gpu", "label_selector": "k=gpu", "gpu_resouce_name": "nvidia.com/gpu"}]"#,
        );
        assert!(
            err.contains("unknown field 'gpu_resouce_name'"),
            "{}",
            err
        );
        assert!(
            err.contains("allowed: name, label_selector, gpu_resource_name"),
            "{}",
            err
        );
    }

    #[test]
    fn duplicate_name_is_rejected() {
        let err = errors_of(
            r#"[{"name": "gpu", "label_selector": "k=a"}, {"name": "gpu", "label_selector": "k=b"}]"#,
        );
        assert!(
            err.contains("flavors[1]: duplicate 'name' value 'gpu'"),
            "{}",
            err
        );
    }

    #[test]
    fn label_selector_without_equals_is_rejected() {
        let err = errors_of(r#"[{"name": "cpu", "label_selector": "cjob.io/flavor"}]"#);
        assert!(err.contains("must be in 'key=value' form"), "{}", err);
    }

    #[test]
    fn label_selector_with_two_equals_is_rejected() {
        let err = errors_of(r#"[{"name": "cpu", "label_selector": "a=b=c"}]"#);
        assert!(err.contains("must be in 'key=value' form"), "{}", err);
    }

    #[test]
    fn label_selector_with_empty_side_is_rejected() {
        let err = errors_of(r#"[{"name": "cpu", "label_selector": "=cpu"}]"#);
        assert!(err.contains("must be in 'key=value' form"), "{}", err);
        let err = errors_of(r#"[{"name": "cpu", "label_selector": "cjob.io/flavor="}]"#);
        assert!(err.contains("must be in 'key=value' form"), "{}", err);
    }

    #[test]
    fn empty_gpu_resource_name_is_rejected() {
        let err = errors_of(r#"[{"name": "gpu", "label_selector": "k=gpu", "gpu_resource_name": ""}]"#);
        assert!(err.contains("'gpu_resource_name' must not be empty"), "{}", err);
    }

    #[test]
    fn image_is_accepted() {
        let value = r#"[
            {"name": "cpu", "label_selector": "k=cpu"},
            {"name": "gpu", "label_selector": "k=gpu", "image": "registry/cuda:1.0"}
        ]"#;
        assert_eq!(validate_resource_flavors(value).unwrap().len(), 2);
    }

    #[test]
    fn image_may_be_omitted_or_null() {
        let value = r#"[
            {"name": "cpu", "label_selector": "k=cpu"},
            {"name": "gpu", "label_selector": "k=gpu", "image": null}
        ]"#;
        assert_eq!(validate_resource_flavors(value).unwrap().len(), 2);
    }

    #[test]
    fn empty_image_is_rejected() {
        let err = errors_of(r#"[{"name": "gpu", "label_selector": "k=gpu", "image": ""}]"#);
        assert!(err.contains("'image' must not be empty"), "{}", err);
    }

    #[test]
    fn non_string_image_is_rejected() {
        let err = errors_of(r#"[{"name": "gpu", "label_selector": "k=gpu", "image": 42}]"#);
        assert!(err.contains("'image' must be a string"), "{}", err);
    }

    #[test]
    fn all_violations_are_reported_together() {
        let value = r#"[
            {"name": "cpu", "label_selector": "cjob.io/flavor=cpu"},
            {"name": "gpu", "label_selector": "cjob.io/flavor=gpu", "gpu_resouce_name": "nvidia.com/gpu"},
            {"name": "gpu", "label_selector": "cjob.io/flavor"}
        ]"#;
        let err = errors_of(value);
        assert!(err.contains("flavors[1]: unknown field 'gpu_resouce_name'"), "{}", err);
        assert!(err.contains("flavors[2]: 'label_selector' must be in 'key=value' form"), "{}", err);
        assert!(err.contains("flavors[2]: duplicate 'name' value 'gpu'"), "{}", err);
    }

    // --- DEFAULT_FLAVOR consistency: setting RESOURCE_FLAVORS ---

    #[test]
    fn setting_flavors_accepts_matching_default() {
        let data = cm(&[("DEFAULT_FLAVOR", "cpu")]);
        let (result, warnings) = check("RESOURCE_FLAVORS", VALID, &data);
        assert!(result.is_ok());
        assert_eq!(warnings, "");
    }

    #[test]
    fn setting_flavors_rejects_orphaned_default() {
        let data = cm(&[("DEFAULT_FLAVOR", "gpu-a100")]);
        let (result, _) = check("RESOURCE_FLAVORS", VALID, &data);
        let err = result.unwrap_err().to_string();
        assert!(err.contains("current 'DEFAULT_FLAVOR' value 'gpu-a100'"), "{}", err);
        assert!(err.contains("available: cpu, gpu"), "{}", err);
    }

    #[test]
    fn setting_flavors_warns_when_default_is_unset() {
        let data = cm(&[]);
        let (result, warnings) = check("RESOURCE_FLAVORS", VALID, &data);
        assert!(result.is_ok());
        assert!(warnings.contains("'DEFAULT_FLAVOR' is not set"), "{}", warnings);
    }

    #[test]
    fn setting_flavors_ignores_surrounding_whitespace_in_default() {
        let data = cm(&[("DEFAULT_FLAVOR", "cpu\n")]);
        let (result, _) = check("RESOURCE_FLAVORS", VALID, &data);
        assert!(result.is_ok());
    }

    // --- DEFAULT_FLAVOR consistency: setting DEFAULT_FLAVOR ---

    #[test]
    fn setting_default_accepts_existing_flavor() {
        let data = cm(&[("RESOURCE_FLAVORS", VALID)]);
        let (result, warnings) = check("DEFAULT_FLAVOR", "gpu", &data);
        assert!(result.is_ok());
        assert_eq!(warnings, "");
    }

    #[test]
    fn setting_default_rejects_unknown_flavor() {
        let data = cm(&[("RESOURCE_FLAVORS", VALID)]);
        let (result, _) = check("DEFAULT_FLAVOR", "gpu-a100", &data);
        let err = result.unwrap_err().to_string();
        assert!(err.contains("'DEFAULT_FLAVOR' value 'gpu-a100'"), "{}", err);
        assert!(err.contains("available: cpu, gpu"), "{}", err);
    }

    #[test]
    fn setting_default_warns_when_flavors_are_unset() {
        let data = cm(&[]);
        let (result, warnings) = check("DEFAULT_FLAVOR", "gpu-a100", &data);
        assert!(result.is_ok());
        assert!(warnings.contains("'RESOURCE_FLAVORS' is not set"), "{}", warnings);
    }

    #[test]
    fn setting_default_warns_when_flavors_are_broken() {
        // A broken RESOURCE_FLAVORS must not block repairing DEFAULT_FLAVOR
        let data = cm(&[("RESOURCE_FLAVORS", "[{")]);
        let (result, warnings) = check("DEFAULT_FLAVOR", "cpu", &data);
        assert!(result.is_ok());
        assert!(
            warnings.contains("not a valid flavor definition list"),
            "{}",
            warnings
        );
    }

    // --- other keys are untouched ---

    #[test]
    fn unrelated_keys_skip_structural_validation() {
        let data = cm(&[("RESOURCE_FLAVORS", VALID), ("DEFAULT_FLAVOR", "cpu")]);
        let (result, warnings) = check("DISPATCH_BATCH_SIZE", "100", &data);
        assert!(result.is_ok());
        assert_eq!(warnings, "");
    }
}
