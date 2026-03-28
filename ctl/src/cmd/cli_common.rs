use anyhow::{bail, Context, Result};
use tokio::process::Command;

pub const PVC_MOUNT_PATH: &str = "/cli-binary";
pub const PVC_CLAIM_NAME: &str = "cli-binary";

pub async fn run_kubectl(args: &[&str]) -> Result<String> {
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

pub async fn cleanup_pod(namespace: &str, pod_name: &str) {
    let _ = run_kubectl(&[
        "delete", "pod", pod_name,
        "--namespace", namespace,
        "--grace-period=0",
        "--force",
        "--ignore-not-found",
    ])
    .await;
}

/// Create a temporary busybox pod with the cli-binary PVC mounted.
/// Returns the pod name on success.
pub async fn create_temp_pod(namespace: &str, purpose: &str) -> Result<String> {
    let timestamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs();
    let pod_name = format!("cjobctl-cli-{}-{}", purpose, timestamp);

    let overrides = serde_json::json!({
        "spec": {
            "containers": [{
                "name": purpose,
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

    Ok(pod_name)
}
