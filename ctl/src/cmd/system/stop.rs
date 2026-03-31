use anyhow::Result;

pub async fn run(
    _k8s_client: &kube::Client,
    _db_client: &tokio_postgres::Client,
    _system_namespace: &str,
    _skip_confirm: bool,
) -> Result<()> {
    todo!("system stop")
}
