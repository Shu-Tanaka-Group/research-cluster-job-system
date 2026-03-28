use anyhow::{bail, Result};

use super::cli_common::{self, PVC_MOUNT_PATH};

pub async fn run(namespace: &str, version: &str) -> Result<()> {
    let pod_name = cli_common::create_temp_pod(namespace, "remove").await?;

    let result = remove_version(namespace, &pod_name, version).await;

    println!("  Cleaning up temporary pod...");
    cli_common::cleanup_pod(namespace, &pod_name).await;

    result
}

async fn remove_version(namespace: &str, pod_name: &str, version: &str) -> Result<()> {
    // Read current latest
    let latest = cli_common::run_kubectl(&[
        "exec", pod_name,
        "--namespace", namespace,
        "--", "cat", &format!("{}/latest", PVC_MOUNT_PATH),
    ])
    .await?;
    let latest = latest.trim();

    // Refuse to delete the latest version
    if version == latest {
        bail!(
            "Cannot remove version {}: it is the current latest. Deploy a different version first.",
            version
        );
    }

    // Check if version directory exists
    let check = cli_common::run_kubectl(&[
        "exec", pod_name,
        "--namespace", namespace,
        "--", "sh", "-c",
        &format!("test -d {}/{} && echo exists || echo missing", PVC_MOUNT_PATH, version),
    ])
    .await?;

    if check.trim() != "exists" {
        bail!("Version {} not found on PVC.", version);
    }

    // Confirmation prompt
    eprint!("Remove CLI v{} from PVC? [y/N] ", version);
    std::io::Write::flush(&mut std::io::stderr())?;
    let mut input = String::new();
    std::io::stdin().read_line(&mut input)?;
    if input.trim().to_lowercase() != "y" {
        println!("Aborted.");
        return Ok(());
    }

    // Delete version directory
    println!("  Removing version {}...", version);
    cli_common::run_kubectl(&[
        "exec", pod_name,
        "--namespace", namespace,
        "--", "rm", "-rf", &format!("{}/{}", PVC_MOUNT_PATH, version),
    ])
    .await?;

    println!("Removed CLI v{}.", version);
    Ok(())
}
