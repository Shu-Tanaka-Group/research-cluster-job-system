use anyhow::{bail, Context, Result};
use std::path::Path;
use tokio::process::Command;

const PVC_MOUNT_PATH: &str = "/cli-binary";
const PVC_CLAIM_NAME: &str = "cli-binary";

async fn run_kubectl(args: &[&str]) -> Result<String> {
    let output = Command::new("kubectl")
        .args(args)
        .output()
        .await
        .context("Failed to run kubectl")?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        bail!("kubectl {} failed: {}", args.first().unwrap_or(&""), stderr.trim());
    }

    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

async fn cleanup_pod(namespace: &str, pod_name: &str) {
    let _ = run_kubectl(&[
        "delete", "pod", pod_name,
        "--namespace", namespace,
        "--grace-period=0",
        "--force",
        "--ignore-not-found",
    ])
    .await;
}

pub async fn run(namespace: &str, binary_path: &str, version: &str) -> Result<()> {
    // Validate binary file exists
    if !Path::new(binary_path).is_file() {
        bail!("Binary file not found: {}", binary_path);
    }

    // Generate unique pod name
    let timestamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs();
    let pod_name = format!("cjobctl-cli-deploy-{}", timestamp);

    println!("Deploying CLI v{} to PVC...", version);

    // Build overrides JSON for PVC mount
    let overrides = serde_json::json!({
        "spec": {
            "containers": [{
                "name": "deploy",
                "image": "busybox",
                "command": ["sleep", "3600"],
                "volumeMounts": [{
                    "name": PVC_CLAIM_NAME,
                    "mountPath": PVC_MOUNT_PATH
                }]
            }],
            "volumes": [{
                "name": PVC_CLAIM_NAME,
                "persistentVolumeClaim": {
                    "claimName": PVC_CLAIM_NAME
                }
            }]
        }
    });
    let overrides_str = overrides.to_string();

    // Start temporary pod
    println!("  Starting temporary pod...");
    let result = run_kubectl(&[
        "run", &pod_name,
        "--namespace", namespace,
        "--image=busybox",
        "--restart=Never",
        "--overrides", &overrides_str,
        "--command", "--", "sleep", "3600",
    ])
    .await;

    if let Err(e) = result {
        cleanup_pod(namespace, &pod_name).await;
        return Err(e);
    }

    // Wait for pod to be ready
    println!("  Waiting for pod to be ready...");
    let wait_result = run_kubectl(&[
        "wait", "--for=condition=Ready",
        &format!("pod/{}", pod_name),
        "--namespace", namespace,
        "--timeout=60s",
    ])
    .await;

    if let Err(e) = wait_result {
        cleanup_pod(namespace, &pod_name).await;
        return Err(e);
    }

    // Execute deployment steps (with cleanup on any failure)
    let deploy_result = deploy_binary(namespace, &pod_name, binary_path, version).await;

    // Always cleanup
    println!("  Cleaning up temporary pod...");
    cleanup_pod(namespace, &pod_name).await;

    deploy_result?;

    println!("CLI v{} deployed successfully.", version);
    Ok(())
}

async fn deploy_binary(
    namespace: &str,
    pod_name: &str,
    binary_path: &str,
    version: &str,
) -> Result<()> {
    // Create version directory
    println!("  Creating directory...");
    run_kubectl(&[
        "exec", pod_name,
        "--namespace", namespace,
        "--", "mkdir", "-p", &format!("{}/{}", PVC_MOUNT_PATH, version),
    ])
    .await?;

    // Copy binary
    println!("  Copying binary...");
    let dest = format!("{}/{}:{}/{}/cjob", namespace, pod_name, PVC_MOUNT_PATH, version);
    run_kubectl(&["cp", binary_path, &dest]).await?;

    // Set executable permission
    println!("  Setting permissions...");
    run_kubectl(&[
        "exec", pod_name,
        "--namespace", namespace,
        "--", "chmod", "+x", &format!("{}/{}/cjob", PVC_MOUNT_PATH, version),
    ])
    .await?;

    // Update latest file
    println!("  Updating latest version...");
    run_kubectl(&[
        "exec", pod_name,
        "--namespace", namespace,
        "--", "sh", "-c", &format!("echo '{}' > {}/latest", version, PVC_MOUNT_PATH),
    ])
    .await?;

    Ok(())
}
