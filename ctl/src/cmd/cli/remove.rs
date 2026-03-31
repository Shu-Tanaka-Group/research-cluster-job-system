use anyhow::{bail, Result};

use super::{cleanup_pod, create_temp_pod, run_kubectl, PVC_MOUNT_PATH};

pub async fn run(namespace: &str, versions: &[String]) -> Result<()> {
    let pod_name = create_temp_pod(namespace, "remove").await?;

    let result = remove_versions(namespace, &pod_name, versions).await;

    println!("  Cleaning up temporary pod...");
    cleanup_pod(namespace, &pod_name).await;

    result
}

async fn remove_versions(namespace: &str, pod_name: &str, versions: &[String]) -> Result<()> {
    // Read current latest
    let latest = run_kubectl(&[
        "exec", pod_name,
        "--namespace", namespace,
        "--", "cat", &format!("{}/latest", PVC_MOUNT_PATH),
    ])
    .await?;
    let latest = latest.trim().to_string();

    // Validate all versions before prompting
    let mut targets = Vec::new();
    for version in versions {
        if version == &latest {
            bail!(
                "Cannot remove version {}: it is the current latest. Deploy a different version first.",
                version
            );
        }

        let check = run_kubectl(&[
            "exec", pod_name,
            "--namespace", namespace,
            "--", "sh", "-c",
            &format!("test -d {}/{} && echo exists || echo missing", PVC_MOUNT_PATH, version),
        ])
        .await?;

        if check.trim() != "exists" {
            bail!("Version {} not found on PVC.", version);
        }

        targets.push(version.as_str());
    }

    // Confirmation prompt
    if targets.len() == 1 {
        eprint!("Remove CLI v{} from PVC? [y/N] ", targets[0]);
    } else {
        eprintln!("The following versions will be removed:");
        for v in &targets {
            eprintln!("  - {}", v);
        }
        eprint!("Remove {} versions from PVC? [y/N] ", targets.len());
    }
    std::io::Write::flush(&mut std::io::stderr())?;
    let mut input = String::new();
    std::io::stdin().read_line(&mut input)?;
    if input.trim().to_lowercase() != "y" {
        println!("Aborted.");
        return Ok(());
    }

    // Delete version directories
    for version in &targets {
        println!("  Removing version {}...", version);
        run_kubectl(&[
            "exec", pod_name,
            "--namespace", namespace,
            "--", "rm", "-rf", &format!("{}/{}", PVC_MOUNT_PATH, version),
        ])
        .await?;
    }

    if targets.len() == 1 {
        println!("Removed CLI v{}.", targets[0]);
    } else {
        println!("Removed {} versions.", targets.len());
    }
    Ok(())
}
