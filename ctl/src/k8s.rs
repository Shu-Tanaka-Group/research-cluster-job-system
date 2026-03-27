use anyhow::{Context, Result};

pub async fn client() -> Result<kube::Client> {
    kube::Client::try_default()
        .await
        .context("Failed to create Kubernetes client (check kubeconfig)")
}
