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
- The CPU and memory recorded in the DB are the effective allocatable after subtracting DaemonSet Pod requests (only CPU and memory are subtracted; GPU is not). `list_pod_for_all_namespaces()` is paginated with `WATCHER_K8S_LIST_PAGE_SIZE` (see §5.2), and for each page, Pods whose `metadata.ownerReferences` contain `kind: DaemonSet`, have `spec.nodeName` set, and whose `status.phase` is `Pending` / `Running` are aggregated per node (raw Pod objects are discarded once each page has been processed). The `spec.containers[].resources.requests` for each Pod are summed and subtracted from `allocatable` (initContainers are excluded). Containers without requests set are treated as 0, and if the subtraction result is negative it is clamped to 0.
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
- Only entries corresponding to user namespaces are processed from the fetch results. From `spec.hard` and `status.used`, it retrieves `requests.cpu`, `requests.memory`, GPU resources (using `gpu_resource_name` from `RESOURCE_FLAVORS` settings), and `count/jobs.batch`. CPU / memory are parsed using `parse_cpu_millicores()` / `parse_memory_mib()`. `count/jobs.batch` is retrieved as an integer only if present in `spec.hard`; otherwise, it is UPSERTed as `NULL`.
- If a user namespace's ResourceQuota is not included in the fetch results, the corresponding DB row is DELETEd. The Dispatcher treats that namespace as having no limit.
- On K8s API error, a log is output and processing is skipped; existing DB data is preserved.
- Rows for namespaces that are no longer user namespaces (label stripped) are DELETEd.

## 2. Necessity

Even though the Dispatcher creates a Job via a DB scan, the subsequent execution state (RUNNING / SUCCEEDED / FAILED) is only finalized on the Kubernetes side.
The Dispatcher alone cannot detect K8s Job completion / failure, so the Watcher is necessary.

## 3. Minimum Algorithm

1. Periodically monitor the Kubernetes Job list using `WATCHER_K8S_LIST_PAGE_SIZE` (§5.5) for pagination, converting each page to lightweight dataclasses (§5.1). **If an API call fails for any page, the entire reconcile cycle is skipped** (Steps 2–8 and DELETING Phase 2 assume the K8s Job list is complete; continuing with an incomplete list would cause Step 8 to wrongly transition healthy jobs to FAILED and DELETING Phase 2 to clean up DB records while K8s Jobs still exist).
2. Interpret the Job's `status.conditions` according to the following rules:

   | K8s Job's `status.conditions` | DB status | Notes |
   |---|---|---|
   | `type: Complete, status: True` | `SUCCEEDED` | |
   | `type: Failed, status: True, reason: DeadlineExceeded` | `FAILED` | Set `last_error` to `"time limit exceeded"` |
   | `type: Failed, status: True` | `FAILED` | Includes Pod exit code non-zero / startup failures |
   | No conditions / Pod is Running | `RUNNING` | On first RUNNING transition, record `started_at`, retrieve `node_name` from all Pods' `spec.nodeName` and record it, and add cumulative consumption to `namespace_daily_usage` (see [database.md](database.md) §5.2) |

   **Completion fallback (usage recording):** Jobs that complete within one scan cycle cannot be observed in RUNNING state by the Watcher, and transition directly from DISPATCHED to SUCCEEDED / FAILED. In this case `started_at` remains NULL, so on completion transition, if `started_at` is NULL, `_record_resource_usage` is called to add usage to `namespace_daily_usage`. `started_at` is kept NULL (since RUNNING was never actually observed). The same fallback applies to sweep jobs.

3. Identify the corresponding `job_id` from the `cjob.io/job-id` and `cjob.io/namespace` labels (matching by `k8s_job_name` is not used).
4. Update the DB state. However, jobs with DB status `CANCELLED` or `DELETING` are not overwritten (the intentional DB state is preserved even if the K8s side has completed / failed). Note that `HELD` jobs are not targeted by this step because their K8s Job has not been created.
5. If a K8s Job exists for a DB job with status `CANCELLED`, delete it (the DB status remains `CANCELLED` even after K8s Job deletion).
6. Process DB jobs with status `DELETING` in two phases:

   **Phase 1 (deletion request):**
   If a corresponding K8s Job exists, delete it (`propagation_policy="Background"` also deletes Pods as a side effect).

   **Phase 2 (completion confirmation and cleanup):**
   In subsequent scan cycles, verify that no corresponding K8s Jobs exist on K8s for all `DELETING` jobs in the namespace. If all K8s Jobs have disappeared, execute the following in a **single transaction**:

   1. Delete all records for the namespace from the `jobs` table (`job_events` are deleted transitively via `ON DELETE CASCADE`).
   2. Reset `user_job_counters.next_id` to 1.

   (If the transaction crashes midway, everything is rolled back and retried in the next cycle.)

   (Since `propagation_policy="Background"` deletion completes asynchronously, Phase 2 must not be executed in the same cycle as Phase 1.)

7. Delete K8s Jobs whose `cjob.io/job-id` label has no corresponding DB record (orphan Jobs).
8. Transition jobs that are DISPATCHED / RUNNING in the DB but whose corresponding K8s Job no longer exists to FAILED (set `last_error` to `"K8s Job not found (TTL expired or manually deleted)"` and `finished_at` to the current time). This provides automatic recovery when DB and K8s state diverge due to automatic K8s Job deletion via `ttlSecondsAfterFinished` or manual deletion.

**Relationship between `ttlSecondsAfterFinished` and scan cycle interval:**

`ttlSecondsAfterFinished` must be set sufficiently longer than the Watcher's scan cycle interval (currently sharing `DISPATCH_BUDGET_CHECK_INTERVAL_SEC`). If the TTL is too short, K8s Jobs that complete during a temporary Watcher stoppage (restart, failure, etc.) may be deleted by the TTL, causing Step 8 to record successfully completed jobs as FAILED. The current settings (TTL 300 seconds vs cycle interval 10 seconds) provide sufficient headroom even for Watcher restarts (typically 1–2 minutes). When changing the TTL or cycle interval, maintain this relationship.

## 4. Sweep Job Monitoring

### 4.1 Index Tracking

On each polling cycle, retrieve `status.completedIndexes` / `status.failedIndexes` / `status.succeeded` / `status.failed` from the K8s API and update the corresponding DB columns.

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

Follows the K8s Job's `status.conditions` (same logic as regular jobs). The final status is determined at the point where a `Complete` or `Failed` condition appears.

- When K8s returns `Complete`: **FAILED** if `failed_count > 0`, **SUCCEEDED** if `failed_count == 0`.
- When K8s returns `Failed` (e.g. `activeDeadlineSeconds` exceeded): **FAILED**.

This ensures that sweeps with partially failed tasks are always treated as FAILED.

### 4.3 Transition to RUNNING

When the first Pod enters RUNNING (K8s Job's `status.active >= 1`), the DB is updated to RUNNING. `started_at` and `node_name` are recorded, same as for regular jobs.

### 4.3.1 Recording node_name

`node_name` is a cumulative list of all node names used throughout the job's execution lifetime. In the DB, it is stored as comma-separated TEXT (e.g. `"node-1,node-2"`). For regular jobs, there is only one Pod, so the result is effectively a single node name, and no branching from sweep jobs is needed.

**Recording trigger conditions:**

1. **On RUNNING transition**: Fetch all Pods of the Job with `CoreV1Api().list_namespaced_pod()` and merge each Pod's `spec.nodeName` into `node_name`.
2. **On sweep `succeeded_count` / `failed_count` change**: Fetch the Pod list, and if there are new node names, add them to `node_name`. By calling the API only when the counters change, rather than every cycle, additional load on K8s API (etcd) is minimized.
3. **Completion fallback**: Jobs that transition directly to SUCCEEDED/FAILED without going through RUNNING attempt to retrieve node names from Pods on the completion transition if `node_name` is not already recorded (Pods remain until `ttlSecondsAfterFinished`).

An append-only recording approach is adopted, so once recorded, node names are never removed. If a Pod starts, completes, and is deleted within a time shorter than the reconcile interval, its node name may be missed. If the Pod has already been deleted, `node_name` remains NULL.

### 4.4 Resource Usage Addition

On RUNNING transition, add `time_limit_seconds × resource_amount × parallelism`. This reflects the maximum concurrent resource usage, ensuring sweep jobs are appropriately weighted in DRF fairness calculations. When RUNNING is not observed and completion occurs directly, the completion fallback in §3 adds usage via the same calculation.

### 4.5 Handling on CANCELLED

Processed with the same flow as regular jobs. The `completed_indexes` / `failed_indexes` for partially completed tasks remain in the DB with the values updated in the last polling cycle.

## 5. Memory Usage Control

The Watcher's reconcile cycle and node_sync cycle retain K8s API responses and DB query results in memory. Memory consumption grows in proportion to the number of jobs and Pods, so OOMKilled events become likely at larger scales. The following strategies suppress peak memory.

### 5.1 K8s Job Fetch Pagination and Lightweight Representation

`BatchV1Api.list_job_for_all_namespaces()` supports pagination via the `limit` / `continue` parameters. The Watcher fetches pages with `WATCHER_K8S_LIST_PAGE_SIZE` (default 500) and extracts only the minimum fields required by reconcile into a lightweight dataclass (`LightK8sJob`) per page. Raw `V1Job` objects are released immediately after extraction so they can be garbage-collected on a per-page basis.

Fields held by `LightK8sJob`:
- `namespace`, `job_id` (extracted from `cjob.io/namespace` / `cjob.io/job-id` labels)
- `name` (`metadata.name`)
- `conditions` (`status.conditions` converted to a tuple of `(type, status, reason)`)
- `active`, `succeeded`, `failed`, `completed_indexes`, `failed_indexes`

`V1Job` information beyond the above (Pod template, full labels, annotations, etc.) is not referenced during reconcile, so it is discarded at the moment of conversion to the lightweight representation. This reduces memory per retained object to roughly 1/10.

During the reconcile cycle, the full list of lightweight representations and `k8s_map` is retained until cycle completion, so pagination alone has limited effect on peak reduction. Combined with the lightweight form, both the peak during API response parsing and the resident memory during the reconcile cycle are suppressed.

**Handling pagination as a whole-cycle failure:** If an `ApiException` occurs mid-page (including `continue` token expiration or transient API Server errors), the entire reconcile cycle is skipped (same as §3 Step 1). Continuing with a partial Job list would risk Step 8 wrongly transitioning healthy jobs to FAILED, so per-page failures are not tolerated.

### 5.2 DaemonSet Pod Fetch Pagination and Per-Page Aggregation

`CoreV1Api.list_pod_for_all_namespaces()` (used by node resource synchronization §1.1) is also paginated. The Watcher fetches pages using the same `WATCHER_K8S_LIST_PAGE_SIZE` and aggregates DaemonSet Pod CPU / memory requests per node from each page. Only the aggregated result is retained; raw Pod objects are discarded per page.

The K8s API does not support direct filtering by ownerReference, so DaemonSet Pods cannot be selected at the API level, but per-page aggregation-and-discard dramatically reduces peak memory compared to retaining the entire Pod list.

If an API call fails mid-page, the entire node_sync cycle is skipped and existing DB data is preserved (consistent with the existing error handling policy in §1.1).

### 5.3 Lightening DB Queries

DB reads during the reconcile cycle suppress resident memory by the following strategies.

- **Fetching DB Jobs corresponding to K8s Jobs**: Restrict to the set of keys `(namespace, job_id)` from `k8s_map` (`tuple_(Job.namespace, Job.job_id).in_(...)`). Compared to the prior approach of fetching all Jobs per namespace, this avoids loading Jobs such as HELD / QUEUED / CANCELLED that reconcile does not use.
- **Fetching DELETING jobs**: A namespace-wide fetch is retained because DELETING Phase 2 requires per-namespace cleanup determination (the number of DELETING jobs is typically small, so memory impact is minor).
- **Step 8 DISPATCHED / RUNNING reconciliation**: For existence checks, only the `(namespace, job_id)` tuples are SELECTed. For Jobs not present in `k8s_map`, a targeted ORM query loads the rows for FAILED transition and event insertion.

### 5.4 Per-Namespace Batching of Pod Fetches

`CoreV1Api.list_namespaced_pod()`, used for `node_name` recording during reconcile, was called per Job with `label_selector=job-name=...` (N+1 API calls with respect to the number of Jobs). This is consolidated into a per-namespace cache.

- The first time a Pod is needed for a namespace within the reconcile cycle, `list_namespaced_pod(namespace, label_selector="job-name")` is called once and a map from `job-name` label to `[node_name, ...]` is constructed.
- Subsequent Jobs in the same namespace are resolved from the cache.
- The cache is discarded at the end of the cycle.

As a result, the number of API calls scales only with the number of namespaces, and at most one `V1PodList` is retained at a time. Pod fetch failures continue to treat the affected Job's node name as empty, and reconcile continues.

### 5.5 Configuration

| Setting | Default | Purpose |
|---|---|---|
| `WATCHER_K8S_LIST_PAGE_SIZE` | 500 | Page size for `list_job_for_all_namespaces()` and `list_pod_for_all_namespaces()`. Larger values reduce the number of pages and API round-trip costs, but increase the response size per page |

This setting is not included in the default Watcher ConfigMap; administrators add it to the overlay ConfigMap as needed (see [deployment.md](../deployment.md) §5).
