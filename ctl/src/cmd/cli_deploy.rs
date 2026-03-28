use anyhow::{bail, Result};
use std::path::Path;

use super::cli_common::{self, PVC_MOUNT_PATH};

pub async fn run(namespace: &str, binary_path: &str, version: &str, release: bool) -> Result<()> {
    let is_prerelease = version.contains('-');

    if release && is_prerelease {
        bail!(
            "Cannot use --release with pre-release version {}. Deploy as a stable version first.",
            version
        );
    }

    if !Path::new(binary_path).is_file() {
        bail!("Binary file not found: {}", binary_path);
    }

    println!("Deploying CLI v{} to PVC...", version);

    let pod_name = cli_common::create_temp_pod(namespace, "deploy").await?;

    let deploy_result = deploy_binary(namespace, &pod_name, binary_path, version, release).await;

    println!("  Cleaning up temporary pod...");
    cli_common::cleanup_pod(namespace, &pod_name).await;

    deploy_result
}

async fn deploy_binary(
    namespace: &str,
    pod_name: &str,
    binary_path: &str,
    version: &str,
    release: bool,
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

    if release {
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

    // run() is async and requires kubectl, so we test the validation logic
    // by calling run() with a non-existent binary to verify early checks.

    #[tokio::test]
    async fn release_with_prerelease_version_errors() {
        let result = run("/tmp", "/nonexistent", "1.3.0-beta.1", true).await;
        let err = result.unwrap_err().to_string();
        assert!(err.contains("Cannot use --release with pre-release version"));
    }

    #[tokio::test]
    async fn stable_without_release_proceeds_to_binary_check() {
        // Without --release, stable version should proceed past validation
        // and fail at binary file check
        let result = run("/tmp", "/nonexistent", "1.3.0", false).await;
        let err = result.unwrap_err().to_string();
        assert!(err.contains("Binary file not found"));
    }

    #[tokio::test]
    async fn prerelease_without_release_proceeds_to_binary_check() {
        let result = run("/tmp", "/nonexistent", "1.3.0-beta.1", false).await;
        let err = result.unwrap_err().to_string();
        assert!(err.contains("Binary file not found"));
    }

    #[tokio::test]
    async fn stable_with_release_proceeds_to_binary_check() {
        let result = run("/tmp", "/nonexistent", "1.3.0", true).await;
        let err = result.unwrap_err().to_string();
        assert!(err.contains("Binary file not found"));
    }
}
