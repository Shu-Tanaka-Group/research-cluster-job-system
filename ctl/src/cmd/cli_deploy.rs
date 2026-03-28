use anyhow::{bail, Result};
use std::path::Path;

use super::cli_common::{self, PVC_MOUNT_PATH};

fn should_update_latest(version: &str, latest_flag: bool) -> bool {
    latest_flag || !version.contains('-')
}

pub async fn run(namespace: &str, binary_path: &str, version: &str, latest_flag: bool) -> Result<()> {
    if !Path::new(binary_path).is_file() {
        bail!("Binary file not found: {}", binary_path);
    }

    let update_latest = should_update_latest(version, latest_flag);

    println!("Deploying CLI v{} to PVC...", version);

    let pod_name = cli_common::create_temp_pod(namespace, "deploy").await?;

    let deploy_result = deploy_binary(namespace, &pod_name, binary_path, version, update_latest).await;

    println!("  Cleaning up temporary pod...");
    cli_common::cleanup_pod(namespace, &pod_name).await;

    deploy_result
}

async fn deploy_binary(
    namespace: &str,
    pod_name: &str,
    binary_path: &str,
    version: &str,
    update_latest: bool,
) -> Result<()> {
    // Create version directory
    println!("  Creating directory...");
    cli_common::run_kubectl(&[
        "exec", pod_name,
        "--namespace", namespace,
        "--", "mkdir", "-p", &format!("{}/{}", PVC_MOUNT_PATH, version),
    ])
    .await?;

    // Copy binary
    println!("  Copying binary...");
    let dest = format!("{}/{}:{}/{}/cjob", namespace, pod_name, PVC_MOUNT_PATH, version);
    cli_common::run_kubectl(&["cp", binary_path, &dest]).await?;

    // Set executable permission
    println!("  Setting permissions...");
    cli_common::run_kubectl(&[
        "exec", pod_name,
        "--namespace", namespace,
        "--", "chmod", "+x", &format!("{}/{}/cjob", PVC_MOUNT_PATH, version),
    ])
    .await?;

    if update_latest {
        // Update latest file
        println!("  Updating latest version...");
        cli_common::run_kubectl(&[
            "exec", pod_name,
            "--namespace", namespace,
            "--", "sh", "-c", &format!("echo '{}' > {}/latest", version, PVC_MOUNT_PATH),
        ])
        .await?;

        println!("Deployed v{} (latest updated)", version);
    } else {
        // Read current latest for informational message
        let current_latest = cli_common::run_kubectl(&[
            "exec", pod_name,
            "--namespace", namespace,
            "--", "cat", &format!("{}/latest", PVC_MOUNT_PATH),
        ])
        .await
        .unwrap_or_else(|_| "unknown".to_string());

        println!("Deployed v{} (latest unchanged: {})", version, current_latest.trim());
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stable_without_flag() {
        assert!(should_update_latest("1.3.0", false));
    }

    #[test]
    fn stable_with_flag() {
        assert!(should_update_latest("1.3.0", true));
    }

    #[test]
    fn prerelease_without_flag() {
        assert!(!should_update_latest("1.3.1-beta.1", false));
    }

    #[test]
    fn prerelease_with_flag() {
        assert!(should_update_latest("1.3.1-beta.1", true));
    }

    #[test]
    fn prerelease_alpha() {
        assert!(!should_update_latest("2.0.0-alpha", false));
    }

    #[test]
    fn prerelease_rc() {
        assert!(!should_update_latest("1.0.0-rc.1", false));
    }
}
