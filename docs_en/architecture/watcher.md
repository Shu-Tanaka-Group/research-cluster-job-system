> *This document was auto-translated from the [Japanese original](../../docs/architecture/watcher.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# Watcher / Reconciler Design

## 1. Role

The Watcher / Reconciler reflects the execution state on the Kubernetes side into the DB.

- Monitoring Job status
- Monitoring Pod status
- Transitioning to `RUNNING` / `SUCCEEDED` / `FAILED`
- Deleting K8s Jobs for `CANCELLED` jobs
- Deleting K8s Jobs, deleting DB records, and resetting counters for `DELETING` jobs
- Detecting orphan Jobs
- Correcting discrepancies between DB and Kubernetes
- Providing Prometheus counter metrics (`cjob_jobs_completed_total`) via the `/metrics` endpoint on `WATCHER_METRICS_PORT`

The Watcher's main loop touches the `/tmp/liveness` file upon completion of each scan cycle. Kubernetes' Liveness probe checks the last modification time of this file to detect loop stoppage and trigger a restart (see [deployment.md](../deployment.md) §13.5).

The Watcher retrieves the namespace directly from the `cjob.io/namespace` label on K8s Jobs, so it does not depend on namespace naming conventions (the Watcher only reads existing labels and does not construct namespace names).

## 1.1 Node Resource Synchronization

The Watcher periodically fetches `allocatable` resources from K8s API nodes and writes them to the `node_resources` table in the DB (see [database.md](database.md) §6).

- The fetch interval is controlled by `NODE_RESOURCE_SYNC_INTERVAL_SEC` (default 300 seconds). It runs once every N cycles of the main loop (which runs at `DISPATCH_BUDGET_CHECK_INTERVAL_SEC` intervals).
- It iterates through each flavor definition in the `RESOURCE_FLAVORS` setting (see [resources.md](resources.md)) and fetches nodes from the K8s API using `label_selector`. Each node is recorded with that flavor definition's `name` as the flavor value.
- The number of GPU resources is retrieved from `status.allocatable` using the `gpu_resource_name` in the flavor definition. Flavors without `gpu_resource_name` set are recorded with 0 GPU count.
- Fetch results from each flavor are merged with deduplication by node name. For nodes matching the labels of multiple flavors, the flavor defined earlier in `RESOURCE_FLAVORS` takes precedence.
- The CPU and memory recorded in the DB are the effective allocatable after subtracting DaemonSet Pod requests (only CPU and memory are subtracted; GPU is not). All Pods are fetched once using `list_pod_for_all_namespaces()`, and Pods that contain `kind: DaemonSet` in `metadata.ownerReferences`, have `spec.nodeName` set, and have a `status.phase` of `Pending` / `Running` are aggregated per node. The `spec.containers[].resources.requests` for each Pod are summed and subtracted from `allocatable` (initContainers are excluded). Containers without requests set are treated as 0, and if the subtraction result is negative it is clamped to 0.
- The first run executes immediately after Watcher startup, and repeats at the configured interval thereafter.
- Nodes that exist in the DB but are no longer present in the node list retrieved from the K8s API (removed or label-stripped) are DELETEd.
- If a K8s API call fails, a log is output and the cycle is skipped; the next cycle will retry (existing DB data is preserved). Even if fetching nodes for a specific flavor fails, node synchronization for other flavors continues. If the DaemonSet Pod fetch API call fails, the entire node synchronization for that cycle is skipped (to avoid writing inaccurate effective allocatable to the DB).

## 1.2 nominalQuota Synchronization

The Watcher periodically fetches the nominalQuota from the ClusterQueue via the K8s API and writes it to the `flavor_quotas` table in the DB (see [database.md](database.md) §7).

- Runs on the same cycle as node resource synchronization (§1.1).
- Fetches the ClusterQueue (`CLUSTER_QUEUE_NAME`, default `cjob-cluster-queue`) using `CustomObjectsApi.get_cluster_custom_object()`.
- For each flavor in `spec.resourceGroups[0].flavors[]`, reads nominalQuota from `resources[]`. Maps resource name `cpu` → cpu column, `memory` → memory column, others → gpu column.
- If a K8s API call fails, a log is output and the cycle is skipped; the next cycle will retry (existing DB data is preserved).

## 1.3 ResourceQuota Synchronization

The Watcher periodically fetches the ResourceQuota usage status of each user namespace from the K8s API and writes it to the `namespace_resource_quotas` table in the DB (see [database.md](database.md) §8).

- Runs at intervals of `RESOURCE_QUOTA_SYNC_INTERVAL_SEC` (default 10 seconds). Operates on a cycle independent from node resource synchronization (§1.1) and nominalQuota synchronization (§1.2).
- Fetches all user namespaces using `CoreV1Api.list_namespace(label_selector=USER_NAMESPACE_LABEL)`. All user namespaces are tracked regardless of whether they have jobs (to capture resource consumption by User Pods such as JupyterHub before job submission).
- Fetches ResourceQuotas for all namespaces in a single API call using `CoreV1Api.list_resource_quota_for_all_namespaces(field_selector="metadata.name=RESOURCE_QUOTA_NAME")`.
- From the results, only those corresponding to user namespaces are processed. `requests.cpu`, `requests.memory`, GPU resources (using `gpu_resource_name` from `RESOURCE_FLAVORS` settings), and `count/jobs.batch` are retrieved from `spec.hard` and `status.used`. CPU / memory are parsed with `parse_cpu_millicores()` / `parse_memory_mib()`. `count/jobs.batch` is retrieved as an integer value only if present in `spec.hard`; if not present, it is UPSERTed as `NULL`.
- If a ResourceQuota corresponding to a user namespace is not included in the results, the corresponding row in the DB is DELETEd. The Dispatcher treats that namespace as having no limits.
- In case of K8s API errors, a log is output and processing is skipped, preserving existing DB data.
- Rows for namespaces that are no longer user namespaces (label removed) are DELETEd.

## 2. Why It Is Needed

Even if the Dispatcher creates a Job via DB scan, the subsequent execution state (RUNNING / SUCCEEDED / FAILED) is only determined on the Kubernetes side.
Since the Dispatcher alone cannot detect K8s Job completion or failure, the Watcher is necessary.

## 3. Minimal Algorithm

1. Periodically monitor the list of Kubernetes Jobs (or use the watch API). **If the API call fails, skip the entire reconcile cycle** (steps 2–8 and DELETING Phase 2 assume the K8s Job list is complete; continuing with an incomplete list risks step 8 incorrectly transitioning normal jobs to FAILED, and DELETING Phase 2 cleaning up the DB while K8s Jobs still remain).
2. Interpret the Job's `status.conditions` using the following rules:

   | K8s Job `status.conditions` | DB status | Notes |
   |---|---|---|
   | `type: Complete, status: True` | `SUCCEEDED` | |
   | `type: Failed, status: True, reason: DeadlineExceeded` | `FAILED` | Sets `last_error` to `"time limit exceeded"` |
   | `type: Failed, status: True` | `FAILED` | Includes non-zero Pod exit codes and startup failures |
   | No conditions / Pod is Running | `RUNNING` | On first RUNNING transition: record `started_at`, get `node_name` from `spec.nodeName` of all Pods, and add accumulated consumption to `namespace_daily_usage` (see [database.md](database.md) §5.2) |

   **Completion fallback (usage recording):** Jobs that complete within a single scan cycle never have RUNNING observed by Watcher and transition directly from DISPATCHED to SUCCEEDED/FAILED. In this case `started_at` remains NULL, so on the completion transition, if `started_at` is NULL, call `_record_resource_usage` to add the consumption to `namespace_daily_usage`. `started_at` is kept as NULL (since RUNNING was never actually observed). The same fallback applies to sweep jobs.

3. Identify the corresponding `job_id` from the `cjob.io/job-id` label and `cjob.io/namespace` label (matching by `k8s_job_name` is not used).
4. Update the DB status. However, do not overwrite jobs whose DB status is `CANCELLED` or `DELETING` (maintain the intentional DB state even if K8s side has completed or failed). Note that `HELD` jobs are not subject to this step as their K8s Job has not been created yet.
5. If a K8s Job exists for a job whose DB status is `CANCELLED`, delete it (the DB status remains `CANCELLED` after K8s Job deletion).
6. Process jobs whose DB status is `DELETING` in two phases:

   **Phase 1 (deletion request):**
   If the corresponding K8s Job exists, delete it (`propagation_policy="Background"` also deletes Pods).

   **Phase 2 (completion check and cleanup):**
   In a subsequent scan cycle, verify that none of the K8s Jobs corresponding to all `DELETING` jobs in the namespace exist on K8s. If all K8s Jobs have disappeared, execute the following in a **single transaction**:

   1. Delete all records for the namespace from the `jobs` table (`job_events` is cascade-deleted via `ON DELETE CASCADE`).
   2. Reset `next_id` to 1 in `user_job_counters`.

   (If a crash occurs mid-transaction, everything is rolled back and re-executed in the next cycle.)

   (`propagation_policy="Background"` deletion completes asynchronously, so Phase 2 must not be executed in the same cycle as Phase 1.)

7. Delete K8s Jobs that are orphaned — i.e., have a `cjob.io/job-id` label but no corresponding DB record.
8. Transition jobs that are DISPATCHED / RUNNING in the DB but have no corresponding K8s Job on K8s to FAILED (set `last_error` to `"K8s Job not found (TTL expired or manually deleted)"` and set `finished_at` to the current time). This automatically repairs discrepancies between DB and K8s caused by automatic K8s Job deletion via `ttlSecondsAfterFinished` or manual deletion.

**Relationship between `ttlSecondsAfterFinished` and scan cycle interval:**

`ttlSecondsAfterFinished` must be set sufficiently longer than the Watcher's scan cycle interval (currently shared with `DISPATCH_BUDGET_CHECK_INTERVAL_SEC`). If the TTL is too short, K8s Jobs that completed during a temporary Watcher stoppage (restart, failure, etc.) may be deleted by TTL, causing step 8 to record normally completed jobs as FAILED. With the current settings (TTL 300s vs. cycle interval 10s), there is sufficient margin even for Watcher restarts (typically 1–2 minutes). When changing TTL or cycle interval, maintain this relationship.

## 4. Monitoring sweep Jobs

### 4.1 Index Tracking

At each polling cycle, fetch `status.completedIndexes` / `status.failedIndexes` / `status.succeeded` / `status.failed` from the K8s API and update the corresponding columns in the DB.

```sql
UPDATE jobs
SET completed_indexes = :completed_indexes,
    failed_indexes = :failed_indexes,
    succeeded_count = :succeeded_count,
    failed_count = :failed_count
WHERE namespace = :namespace
  AND job_id = :job_id;
```

### 4.2 State Transition Determination

Follow K8s Job `status.conditions` (same logic as regular jobs). The final status is determined when a `Complete` or `Failed` condition appears.

- If K8s returns `Complete`: **FAILED** if `failed_count > 0`, **SUCCEEDED** if `failed_count == 0`
- If K8s returns `Failed` (e.g., `activeDeadlineSeconds` exceeded): **FAILED**

This means a sweep with any partially failed tasks is always treated as FAILED.

### 4.3 Transition to RUNNING

When the first Pod becomes RUNNING (K8s Job's `status.active >= 1`), update the DB to RUNNING. Record `started_at` and `node_name` the same as regular jobs.

### 4.3.1 Recording node_name

`node_name` is a cumulative list of all node names used throughout the job's execution period. It is stored in the DB as comma-separated TEXT (e.g., `"node-1,node-2"`). Regular jobs have a single Pod, so the result is effectively a single node name, and no branching from sweep jobs is needed.

**Trigger conditions for recording:**

1. **On RUNNING transition**: Fetch all Pods for the Job using `CoreV1Api().list_namespaced_pod()` and merge each Pod's `spec.nodeName` into `node_name`.
2. **When sweep's `succeeded_count` / `failed_count` changes**: Fetch the Pod list and add any new node names to `node_name`. The API is called only when counters change, not every cycle, to minimize additional load on the K8s API (etcd).
3. **Completion fallback**: Jobs that transition directly to SUCCEEDED/FAILED without going through RUNNING will attempt to retrieve `node_name` from Pods at the time of completion transition if `node_name` has not been recorded (Pods persist for the duration of `ttlSecondsAfterFinished`).

An append-only approach is used; once recorded, node names are not deleted. If a Pod starts, completes, and is deleted in less time than the reconcile interval, that node name may be missed. If the Pod has already been deleted, `node_name` remains NULL.

### 4.4 Adding Resource Usage

On RUNNING transition, add `time_limit_seconds × resource amount × parallelism`. This reflects the maximum amount of resources used simultaneously, so that sweep jobs are appropriately weighted in DRF fairness calculations. If a job completes without RUNNING being observed, the same calculation is used via the completion fallback described in §3.

### 4.5 Processing on CANCEL

Processed in the same flow as regular jobs. The `completed_indexes` / `failed_indexes` of partially completed tasks retain the values last updated in the previous polling cycle in the DB.
