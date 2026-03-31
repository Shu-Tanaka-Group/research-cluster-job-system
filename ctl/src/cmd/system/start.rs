use anyhow::Result;

pub async fn run(
    _k8s_client: &kube::Client,
    _system_namespace: &str,
    _submit_api_replicas: i32,
) -> Result<()> {
    todo!("system start")
}
