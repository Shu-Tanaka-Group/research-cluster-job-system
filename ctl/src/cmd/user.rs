use anyhow::{bail, Result};
use k8s_openapi::api::core::v1::Namespace;
use kube::api::{ListParams, Patch, PatchParams};
use kube::Api;

pub async fn list(k8s_client: &kube::Client, enabled: bool, disabled: bool) -> Result<()> {
    let ns_api: Api<Namespace> = Api::all(k8s_client.clone());

    let selector = if enabled {
        "type=user,cjob.io/user-namespace=true".to_string()
    } else {
        "type=user".to_string()
    };

    let lp = ListParams::default().labels(&selector);
    let ns_list = ns_api.list(&lp).await?;

    let mut entries: Vec<(String, String, bool)> = Vec::new();

    for ns in &ns_list.items {
        let name = ns.metadata.name.as_deref().unwrap_or("-");
        let labels = ns.metadata.labels.as_ref();
        let annotations = ns.metadata.annotations.as_ref();

        let is_enabled = labels
            .and_then(|l| l.get("cjob.io/user-namespace"))
            .map(|v| v == "true")
            .unwrap_or(false);

        if disabled && is_enabled {
            continue;
        }

        let username = annotations
            .and_then(|a| a.get("cjob.io/username"))
            .map(|s| s.as_str())
            .unwrap_or("-");

        entries.push((name.to_string(), username.to_string(), is_enabled));
    }

    entries.sort_by(|a, b| a.0.cmp(&b.0));

    if entries.is_empty() {
        println!("No user namespaces found.");
        return Ok(());
    }

    println!("{:<25} {:<20} {}", "NAMESPACE", "USERNAME", "ENABLED");
    for (name, username, is_enabled) in &entries {
        println!("{:<25} {:<20} {}", name, username, is_enabled);
    }

    Ok(())
}

pub async fn enable(k8s_client: &kube::Client, namespaces: &[String]) -> Result<()> {
    validate_user_namespaces(k8s_client, namespaces).await?;
    let ns_api: Api<Namespace> = Api::all(k8s_client.clone());
    for ns in namespaces {
        patch_user_namespace_label(&ns_api, ns, "true").await?;
        println!("Enabled CJob for namespace '{}'.", ns);
    }
    Ok(())
}

pub async fn disable(k8s_client: &kube::Client, namespaces: &[String]) -> Result<()> {
    validate_user_namespaces(k8s_client, namespaces).await?;
    let ns_api: Api<Namespace> = Api::all(k8s_client.clone());
    for ns in namespaces {
        patch_user_namespace_label(&ns_api, ns, "false").await?;
        println!("Disabled CJob for namespace '{}'.", ns);
    }
    Ok(())
}

async fn validate_user_namespaces(
    k8s_client: &kube::Client,
    namespaces: &[String],
) -> Result<()> {
    let ns_api: Api<Namespace> = Api::all(k8s_client.clone());
    let mut invalid = Vec::new();

    for ns in namespaces {
        let namespace = ns_api.get(ns).await?;
        let has_user_label = namespace
            .metadata
            .labels
            .as_ref()
            .and_then(|l| l.get("type"))
            .map(|v| v == "user")
            .unwrap_or(false);
        if !has_user_label {
            invalid.push(ns.as_str());
        }
    }

    if !invalid.is_empty() {
        bail!(
            "The following namespace(s) do not have label type=user: {}",
            invalid.join(", ")
        );
    }

    Ok(())
}

async fn patch_user_namespace_label(
    ns_api: &Api<Namespace>,
    namespace: &str,
    value: &str,
) -> Result<()> {
    let patch = serde_json::json!({
        "metadata": {
            "labels": {
                "cjob.io/user-namespace": value
            }
        }
    });
    ns_api
        .patch(namespace, &PatchParams::default(), &Patch::Merge(&patch))
        .await?;
    Ok(())
}
