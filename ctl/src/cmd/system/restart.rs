use anyhow::Result;

pub async fn run(
    _k8s_client: &kube::Client,
    _system_namespace: &str,
    _component: &str,
) -> Result<()> {
    todo!("system restart")
}
