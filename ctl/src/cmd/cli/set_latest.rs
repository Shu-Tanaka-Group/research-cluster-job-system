use anyhow::{bail, Result};

use super::{cleanup_pod, create_temp_pod, run_kubectl, PVC_MOUNT_PATH};

pub async fn run(namespace: &str, version: &str) -> Result<()> {
    if version.contains('-') {
        bail!(
            "Cannot set pre-release version {} as latest.",
            version
        );
    }

    let pod_name = create_temp_pod(namespace, "set-latest").await?;

    let result = set_latest(namespace, &pod_name, version).await;

    println!("  Cleaning up temporary pod...");
    cleanup_pod(namespace, &pod_name).await;

    result
}

async fn set_latest(namespace: &str, pod_name: &str, version: &str) -> Result<()> {
    // Check if version directory exists
    let check = run_kubectl(&[
        "exec", pod_name,
        "--namespace", namespace,
        "--", "sh", "-c",
        &format!("test -d {}/{} && echo exists || echo missing", PVC_MOUNT_PATH, version),
    ])
    .await?;

    if check.trim() != "exists" {
        bail!("Version {} not found on PVC. Deploy it first.", version);
    }

    // Read current latest
    let current_latest = run_kubectl(&[
        "exec", pod_name,
        "--namespace", namespace,
        "--", "cat", &format!("{}/latest", PVC_MOUNT_PATH),
    ])
    .await
    .unwrap_or_else(|_| "unknown".to_string());
    let current_latest = current_latest.trim();

    if current_latest == version {
        println!("Version {} is already the latest.", version);
        return Ok(());
    }

    // Update latest file
    println!("  Updating latest: {} -> {}...", current_latest, version);
    run_kubectl(&[
        "exec", pod_name,
        "--namespace", namespace,
        "--", "sh", "-c", &format!("echo '{}' > {}/latest", version, PVC_MOUNT_PATH),
    ])
    .await?;

    println!("Latest updated to v{}.", version);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn prerelease_version_errors() {
        let result = run("/tmp", "1.3.0-beta.1").await;
        let err = result.unwrap_err().to_string();
        assert!(err.contains("Cannot set pre-release version"));
    }

    #[tokio::test]
    async fn prerelease_rc_errors() {
        let result = run("/tmp", "1.0.0-rc.1").await;
        let err = result.unwrap_err().to_string();
        assert!(err.contains("Cannot set pre-release version"));
    }
}
