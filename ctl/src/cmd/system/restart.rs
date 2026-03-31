use anyhow::{Context, Result};
use k8s_openapi::api::apps::v1::Deployment;
use kube::api::{Patch, PatchParams};
use kube::Api;

pub async fn run(
    k8s_client: &kube::Client,
    system_namespace: &str,
    component: &str,
) -> Result<()> {
    super::validate_component(component)?;

    let deployments: Api<Deployment> = Api::namespaced(k8s_client.clone(), system_namespace);
    let now = chrono::Utc::now().to_rfc3339();
    let patch = serde_json::json!({
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": now
                    }
                }
            }
        }
    });
    deployments
        .patch(component, &PatchParams::default(), &Patch::Merge(&patch))
        .await
        .with_context(|| format!("Failed to restart deployment '{}'", component))?;

    println!(
        "Restarting {}... (use 'cjobctl system status' to check)",
        component
    );
    Ok(())
}
