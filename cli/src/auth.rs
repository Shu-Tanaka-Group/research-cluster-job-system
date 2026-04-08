use anyhow::{Context, Result};
use std::fs;

const TOKEN_PATH: &str = "/var/run/secrets/kubernetes.io/serviceaccount/token";
const NAMESPACE_PATH: &str = "/var/run/secrets/kubernetes.io/serviceaccount/namespace";

pub fn read_token() -> Result<String> {
    fs::read_to_string(TOKEN_PATH)
        .map(|s| s.trim().to_string())
        .context("failed to read ServiceAccount token. Please run inside a Kubernetes Pod")
}

#[allow(dead_code)]
pub fn read_namespace() -> Result<String> {
    fs::read_to_string(NAMESPACE_PATH)
        .map(|s| s.trim().to_string())
        .context("failed to read namespace. Please run inside a Kubernetes Pod")
}
