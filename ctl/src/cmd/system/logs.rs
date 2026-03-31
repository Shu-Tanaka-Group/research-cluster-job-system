use anyhow::Result;
use k8s_openapi::api::core::v1::Pod;
use kube::api::{ListParams, LogParams};
use kube::Api;

pub async fn run(k8s_client: &kube::Client, namespace: &str, component: &str, tail: i64) -> Result<()> {
    super::validate_component(component)?;

    let pods: Api<Pod> = Api::namespaced(k8s_client.clone(), namespace);
    let lp = ListParams::default().labels(&format!("app={}", component));
    let pod_list = pods.list(&lp).await?;

    if pod_list.items.is_empty() {
        println!("No pods found for component '{}'.", component);
        return Ok(());
    }

    for pod in &pod_list.items {
        let pod_name = pod
            .metadata
            .name
            .as_deref()
            .unwrap_or("<unknown>");

        println!("=== {} ===", pod_name);

        let log_params = LogParams {
            tail_lines: Some(tail),
            ..Default::default()
        };
        let log_output = pods.logs(pod_name, &log_params).await?;
        println!("{}", log_output);
    }
    Ok(())
}
