use std::io::{self, Write};

use anyhow::Result;
use k8s_openapi::api::batch::v1::Job;
use kube::api::{DeleteParams, ListParams};
use kube::Api;

use super::{
    scale_deployment, DEPLOYMENT_DISPATCHER, DEPLOYMENT_SUBMIT_API, DEPLOYMENT_WATCHER,
};

pub async fn run(
    k8s_client: &kube::Client,
    db_client: &tokio_postgres::Client,
    system_namespace: &str,
    skip_confirm: bool,
) -> Result<()> {
    // Step 1: Pre-flight check
    let counts = db_client
        .query_one(
            "SELECT \
                COUNT(*) FILTER (WHERE status = 'QUEUED') AS queued, \
                COUNT(*) FILTER (WHERE status = 'DISPATCHING') AS dispatching, \
                COUNT(*) FILTER (WHERE status = 'DISPATCHED') AS dispatched, \
                COUNT(*) FILTER (WHERE status = 'RUNNING') AS running, \
                COUNT(*) FILTER (WHERE status = 'HELD') AS held \
             FROM jobs \
             WHERE status IN ('QUEUED', 'DISPATCHING', 'DISPATCHED', 'RUNNING', 'HELD')",
            &[],
        )
        .await?;

    let queued: i64 = counts.get("queued");
    let dispatching: i64 = counts.get("dispatching");
    let dispatched: i64 = counts.get("dispatched");
    let running: i64 = counts.get("running");
    let held: i64 = counts.get("held");
    let total = queued + dispatching + dispatched + running + held;

    let revert_count = dispatching + dispatched;

    println!(
        "Active jobs: {} (QUEUED: {}, DISPATCHING: {}, DISPATCHED: {}, RUNNING: {}, HELD: {})",
        total, queued, dispatching, dispatched, running, held
    );
    println!("This will:");
    println!("  - Scale down submit-api, dispatcher, watcher to 0 replicas");
    if revert_count > 0 {
        println!(
            "  - Revert {} DISPATCHING/DISPATCHED job(s) to QUEUED",
            revert_count
        );
    }
    if running > 0 {
        println!(
            "  - Fail {} RUNNING job(s) (last_error: system shutdown)",
            running
        );
    }
    println!("  - Delete K8s Jobs in all user namespaces");
    let queued_after = queued + revert_count;
    if queued_after > 0 {
        println!(
            "  - {} QUEUED job(s) will be re-dispatched on next start",
            queued_after
        );
    }
    if held > 0 {
        println!(
            "  - {} HELD job(s) will remain held (use cjob release to resume)",
            held
        );
    }

    if !skip_confirm {
        print!("Proceed? [y/N] ");
        io::stdout().flush()?;
        let mut input = String::new();
        io::stdin().read_line(&mut input)?;
        if !input.trim().eq_ignore_ascii_case("y") {
            println!("Cancelled.");
            return Ok(());
        }
    }

    // Step 2: Scale down Submit API
    scale_deployment(k8s_client, system_namespace, DEPLOYMENT_SUBMIT_API, 0).await?;
    println!("Scaled down {} to 0 replicas.", DEPLOYMENT_SUBMIT_API);

    // Step 3: Scale down Dispatcher (before DB changes to prevent re-dispatch)
    scale_deployment(k8s_client, system_namespace, DEPLOYMENT_DISPATCHER, 0).await?;
    println!("Scaled down {} to 0 replicas.", DEPLOYMENT_DISPATCHER);

    // Step 4: Update job states in DB
    let reverted_dispatching = db_client
        .execute(
            "UPDATE jobs SET status = 'QUEUED', retry_after = NULL, retry_count = 0 \
             WHERE status = 'DISPATCHING'",
            &[],
        )
        .await?;
    if reverted_dispatching > 0 {
        println!("Reverted {} DISPATCHING job(s) to QUEUED.", reverted_dispatching);
    }

    let reverted_dispatched = db_client
        .execute(
            "UPDATE jobs SET status = 'QUEUED' WHERE status = 'DISPATCHED'",
            &[],
        )
        .await?;
    if reverted_dispatched > 0 {
        println!("Reverted {} DISPATCHED job(s) to QUEUED.", reverted_dispatched);
    }

    let failed_running = db_client
        .execute(
            "UPDATE jobs SET status = 'FAILED', last_error = 'system shutdown', finished_at = NOW() \
             WHERE status = 'RUNNING'",
            &[],
        )
        .await?;
    if failed_running > 0 {
        println!("Failed {} RUNNING job(s).", failed_running);
    }

    // Step 5: Delete K8s Jobs
    let deleted = delete_all_cjob_k8s_jobs(k8s_client).await?;
    if deleted > 0 {
        println!("Deleted {} K8s Job(s).", deleted);
    } else {
        println!("No K8s Jobs to delete.");
    }

    // Step 6: Scale down Watcher
    scale_deployment(k8s_client, system_namespace, DEPLOYMENT_WATCHER, 0).await?;
    println!("Scaled down {} to 0 replicas.", DEPLOYMENT_WATCHER);

    println!("CJob system stopped. PostgreSQL remains running.");
    Ok(())
}

async fn delete_all_cjob_k8s_jobs(k8s_client: &kube::Client) -> Result<u64> {
    let jobs: Api<Job> = Api::all(k8s_client.clone());
    let lp = ListParams::default().labels("cjob.io/job-id");
    let job_list = jobs.list(&lp).await?;

    let mut deleted = 0u64;
    let dp = DeleteParams {
        propagation_policy: Some(kube::api::PropagationPolicy::Background),
        ..Default::default()
    };

    for job in &job_list.items {
        let name = match &job.metadata.name {
            Some(n) => n,
            None => continue,
        };
        let ns = match &job.metadata.namespace {
            Some(n) => n,
            None => continue,
        };

        let ns_jobs: Api<Job> = Api::namespaced(k8s_client.clone(), ns);
        match ns_jobs.delete(name, &dp).await {
            Ok(_) => deleted += 1,
            Err(e) => {
                eprintln!("Warning: Failed to delete K8s Job {}/{}: {}", ns, name, e);
            }
        }
    }

    Ok(deleted)
}
