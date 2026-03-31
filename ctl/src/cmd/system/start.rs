use anyhow::Result;

use super::{
    scale_deployment, DEFAULT_DISPATCHER_REPLICAS, DEFAULT_WATCHER_REPLICAS,
    DEPLOYMENT_DISPATCHER, DEPLOYMENT_SUBMIT_API, DEPLOYMENT_WATCHER,
};

pub async fn run(
    k8s_client: &kube::Client,
    system_namespace: &str,
    submit_api_replicas: i32,
) -> Result<()> {
    scale_deployment(
        k8s_client,
        system_namespace,
        DEPLOYMENT_DISPATCHER,
        DEFAULT_DISPATCHER_REPLICAS,
    )
    .await?;
    println!(
        "Scaled up {} to {} replica(s).",
        DEPLOYMENT_DISPATCHER, DEFAULT_DISPATCHER_REPLICAS
    );

    scale_deployment(
        k8s_client,
        system_namespace,
        DEPLOYMENT_WATCHER,
        DEFAULT_WATCHER_REPLICAS,
    )
    .await?;
    println!(
        "Scaled up {} to {} replica(s).",
        DEPLOYMENT_WATCHER, DEFAULT_WATCHER_REPLICAS
    );

    scale_deployment(
        k8s_client,
        system_namespace,
        DEPLOYMENT_SUBMIT_API,
        submit_api_replicas,
    )
    .await?;
    println!(
        "Scaled up {} to {} replica(s).",
        DEPLOYMENT_SUBMIT_API, submit_api_replicas
    );

    println!("CJob system started. Use 'cjobctl system status' to check pod status.");
    Ok(())
}
