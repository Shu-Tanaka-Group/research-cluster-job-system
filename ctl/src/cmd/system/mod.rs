pub mod logs;
pub mod restart;
pub mod start;
pub mod status;
pub mod stop;

use anyhow::{bail, Context, Result};
use k8s_openapi::api::apps::v1::Deployment;
use kube::api::{Patch, PatchParams};
use kube::Api;

pub const DEPLOYMENT_SUBMIT_API: &str = "submit-api";
pub const DEPLOYMENT_DISPATCHER: &str = "dispatcher";
pub const DEPLOYMENT_WATCHER: &str = "watcher";

pub const VALID_COMPONENTS: &[&str] = &[DEPLOYMENT_DISPATCHER, DEPLOYMENT_WATCHER, DEPLOYMENT_SUBMIT_API];

pub const DEFAULT_DISPATCHER_REPLICAS: i32 = 1;
pub const DEFAULT_WATCHER_REPLICAS: i32 = 1;
pub const DEFAULT_SUBMIT_API_REPLICAS: i32 = 2;

pub async fn scale_deployment(
    k8s_client: &kube::Client,
    namespace: &str,
    name: &str,
    replicas: i32,
) -> Result<()> {
    let deployments: Api<Deployment> = Api::namespaced(k8s_client.clone(), namespace);
    let patch = serde_json::json!({
        "spec": {
            "replicas": replicas
        }
    });
    deployments
        .patch(name, &PatchParams::default(), &Patch::Merge(&patch))
        .await
        .with_context(|| format!("Failed to scale deployment '{}'", name))?;
    Ok(())
}

pub fn validate_component(component: &str) -> Result<()> {
    if !VALID_COMPONENTS.contains(&component) {
        bail!(
            "Invalid component '{}'. Valid: {}",
            component,
            VALID_COMPONENTS.join(", ")
        );
    }
    Ok(())
}
