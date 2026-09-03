> *This document was auto-translated from the [Japanese original](../../docs/architecture/watcher.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# Watcher / Reconciler Design

## 1. Role

The Watcher / Reconciler reflects the execution state on the Kubernetes side into the DB.

- Monitoring Job status
- Monitoring Pod status
- Transitioning to `RUNNING` / `SUCCEEDED` / `FAILED`
- Enforcing the time limit (`time_limit_seconds`)
- Requeueing jobs stalled in `DISPATCHED` back to `QUEUED` (guard for unschedulable jobs)
- Deleting K8s Jobs for `CANCELLED` jobs
- Deleting K8s Jobs, deleting DB records, and resetting counters for `DELETING` jobs
- Detecting orphan Jobs
- Correcting discrepancies between DB and Kubernetes
- Providing Prometheus counter metrics (`cjob_jobs_completed_total` / `cjob_jobs_unschedulable_requeued_total`) via the `/metrics` endpoint on `WATCHER_METRICS_PORT`

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

1. Periodically monitor the Kubernetes Job list using `WATCHER_K8S_LIST_PAGE_SIZE` (§5.5) for pagination, converting each page to lightweight dataclasses (§5.1). **If an API call fails for any page, the entire reconcile cycle is skipped** (Steps 2–9 and DELETING Phase 2 assume the K8s Job list is complete; continuing with an incomplete list would cause Step 8 to wrongly transition healthy jobs to FAILED and DELETING Phase 2 to clean up DB records while K8s Jobs still exist).
2. Interpret the Job's `status.conditions` according to the following rules:

   | K8s Job's `status.conditions` | DB status | Notes |
   |---|---|---|
   | `type: Complete, status: True` | `SUCCEEDED` | |
   | `type: Failed, status: True, reason: DeadlineExceeded` | `FAILED` | Set `last_error` to `"time limit exceeded"`. cjob does not set `activeDeadlineSeconds` on K8s Jobs (the time limit is enforced in §3 Step 9), so this condition only appears for Jobs created before this change or Jobs given an `activeDeadlineSeconds` manually |
   | `type: Failed, status: True` | `FAILED` | Includes Pod exit code non-zero / startup failures |
   | No conditions, `status.active > 0` and `status.ready > 0` | `RUNNING` | On first RUNNING transition, record `started_at`, retrieve `node_name` from all Pods' `spec.nodeName` and record it, and add cumulative consumption to `namespace_daily_usage` (see [database.md](database.md) §5.2) |

   **Handling Pending Pods:** A K8s Job's `status.active` is the count of "pending and running pods which are not terminating", so a Job whose Pod stays Pending due to insufficient node resources still reports `active > 0`. To avoid misclassifying a Pending Pod as RUNNING, combine the check with `status.ready` (Pods whose `Ready` condition is True, provided by the `JobReadyPods` feature that reached GA in K8s 1.26) to confirm that at least one Pod actually has its containers running. cjob jobs do not define readiness probes, so the kubelet's default behavior sets Pod Ready to True once all containers are Running; thus `ready > 0` is equivalent to "at least one Pod's containers are Running". `status.ready` is already included in the `list_job_for_all_namespaces()` response, so no additional K8s API / etcd load is incurred (for the minimum K8s version requirement, see [prerequisites.md](prerequisites.md) §1).

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

   **Dispatcher grace period:** DISPATCHED jobs whose `dispatched_at` is newer than `NOW() - WATCHER_DISPATCH_GRACE_SEC` are excluded from this check. When the Dispatcher creates a job immediately after the Watcher snapshots the K8s Job list at the start of a reconcile cycle, that cycle's list will not yet contain the newly-created Job; without this guard, freshly dispatched jobs would be incorrectly recorded as FAILED. The grace period is not applied to RUNNING jobs (once observed, their K8s Job will remain visible in subsequent cycles, so disappearance indicates a real deletion).

9. Terminate jobs whose execution time exceeded the time limit (time limit enforcement)

   The K8s Job's `activeDeadlineSeconds` is not used; instead the Watcher enforces the time limit measured from `started_at`. K8s measures from `.status.startTime`, which is fixed at the moment Kueue unsuspends the Job, so the time the Pod subsequently spends Pending while kube-scheduler waits for a node to free up would be counted against the time limit. To avoid this, the time at which execution actually began (`started_at`) is used as the origin.

   **Targets:** jobs with `status = 'RUNNING'` and `started_at IS NOT NULL` and `started_at + time_limit_seconds < NOW()`

   **Order of operations:** delete the K8s Job with `propagation_policy="Background"` (Pods are deleted along with it and terminate after SIGTERM plus the grace period), and update the DB **only after the deletion succeeds (including 404)**. Set `status` to `FAILED`, `last_error` to `"time limit exceeded"`, and `finished_at` to the current time; append `FAILED` to `job_events`; and increment `cjob_jobs_completed_total{status="failed"}`. Usage (`namespace_daily_usage`) is not added, because it was already recorded on the RUNNING transition.

   Deletion comes first because in the reverse order a failed deletion would leave the K8s Job alive, and if that job then ran to completion Step 4 would overwrite the DB with `SUCCEEDED` (the terminal-state regression guard only blocks transitions to RUNNING). With deletion first, a job whose deletion failed stays RUNNING and is retried on the next cycle.

   If the Watcher crashes after a successful deletion but before the DB commit, the next cycle's Step 8 picks the job up and `last_error` becomes `"K8s Job not found (TTL expired or manually deleted)"`. The window is on the order of milliseconds and the final state (FAILED) is correct, so this is acceptable.

   **Execution position:** run after all of Step 4's status synchronization is complete, and before Step 8's disappearance check. Running it after Step 4 avoids deleting jobs that naturally transitioned to SUCCEEDED / FAILED in the same cycle. Running it before Step 8 keeps the jobs it just marked FAILED from being picked up twice.

   **Expected constraints:**

   - Detection granularity equals the scan cycle interval (`DISPATCH_BUDGET_CHECK_INTERVAL_SEC`, default 10 seconds). This is a negligible error relative to a 24-hour time limit
   - If the Watcher is stopped for a long period, jobs past their time limit keep running. The liveness probe automatically recovers a stalled loop, but a crash loop is not recovered. The impact is limited to resource occupation; no data loss occurs
   - `started_at` is the time the Watcher observed RUNNING, which lags the container's actual start time by at most one scan cycle. This works in the user's favor, so it is acceptable
   - K8s Jobs created before this change keep running with their `activeDeadlineSeconds`, so jobs already running at rollout time retain the old behavior (see [migration](../migration.md))

10. Requeue jobs stalled in `DISPATCHED` back to `QUEUED` (guard for unschedulable jobs)

    cjob does not set `activeDeadlineSeconds` on K8s Jobs (Step 9), so there is no mechanism to terminate a job pinned in `DISPATCHED` because it cannot be placed on any node. Left alone, such a job occupies ClusterQueue quota and namespace ResourceQuota indefinitely, blocking admission of other users' jobs. The Dispatcher's per-node bin-packing pre-check ([dispatcher.md](dispatcher.md) §2.6) lowers the probability of a stall, but stalls remain possible because kube-scheduler's choice cannot be predicted exactly and because `node_resources` is synced with a delay (§2.6.5). This step is the last line of defence.

    **Targets:** jobs satisfying all of the following

    - `status = 'DISPATCHED'`
    - `dispatched_at IS NOT NULL` and `dispatched_at + WATCHER_DISPATCH_TIMEOUT_SEC < NOW()`
    - a corresponding K8s Job exists in the K8s Job list fetched this cycle

    Restricting to jobs whose K8s Job exists is required because a job whose K8s Job has disappeared falls under Step 8 (FAILED transition); requeueing it here would disable that self-healing path.

    **The decision is based purely on elapsed time.** Pod `status.conditions` (`PodScheduled=False` / reason `Unschedulable`) is not inspected, for the following reasons.

    - No additional API cost for fetching Pods
    - Stalls where Kueue has not admitted the job (no Pod exists yet) are rescued through the same path. For a last line of defence, catching stalls regardless of cause is more robust
    - A requeue is a harmless operation that never loses the job, so the accuracy requirement on the decision is low. Setting the threshold long enough avoids catching legitimate startup delays such as image pulls or PVC attach waits

    **Processing order:** as in Step 9, the K8s Job is deleted with `propagation_policy="Background"` and the DB is updated **only after the deletion is confirmed to have succeeded (404 included)**. A job whose deletion fails stays `DISPATCHED` and is retried on the next cycle. The reverse order would let a surviving K8s Job transition to `RUNNING` / `SUCCEEDED` afterwards, so a job that was supposed to be back in `QUEUED` would run twice.

    **DB update content:**

    - Roll `status` back to `QUEUED`
    - Increment `unschedulable_count` by 1 (see [database.md](database.md) §1)
    - Set `retry_after` to `NOW() + min(WATCHER_DISPATCH_TIMEOUT_SEC × 2^(unschedulable_count - 1), WATCHER_DISPATCH_BACKOFF_MAX_SEC)` (exponential backoff)
    - Append `UNSCHEDULABLE` to `job_events` (with the wait time and requeue count in `payload_json`, see [database.md](database.md) §3.1)
    - Increment `cjob_jobs_unschedulable_requeued_total`

    `retry_count` is not incremented. A stall stems from a lack of free capacity on the cluster side rather than a failure of the job itself, so it must not consume the `DISPATCH_MAX_RETRIES` retry budget (the same reasoning as `DEFERRED` for ResourceQuota races, see [dispatcher.md](dispatcher.md) §2.5). `k8s_job_name` / `dispatched_at` are left as they are (the Dispatcher overwrites them on re-dispatch). Usage (`namespace_daily_usage`) is not added because the job never ran.

    **Suppressing infinite loops:** a requeued job becomes a target of the Dispatcher's candidate query on the next cycle, but `retry_after` excludes it from the candidates while the backoff is in effect ([dispatcher.md](dispatcher.md) §1.2). In structural cases where the per-node bin-packing pre-check keeps judging nodes as free (occupation by non-cjob Pods, PVC node affinity, max-pods, etc.), the job enters a dispatch → timeout → requeue loop, but because the backoff grows exponentially the waste of cycles and resources decays, converging on "wait until the cluster frees up" behavior. The job lives on as `QUEUED`, so it is never abandoned and lost.

    **Why no new state is added:** a dedicated state (e.g. `BLOCKED`) or a transition to `HELD` are alternatives, but both move in the direction of "stopping the job", which is out of proportion with the cause residing on the cluster side. `QUEUED` + `retry_after` has no ripple effect on the Dispatcher / CLI / cjobctl / Grafana and requires no follow-up in every layer that enumerates statuses. If operations reveal that jobs waiting in backoff actually accumulate and need dedicated visualization, this can be reconsidered then.

    **Execution position:** run after Step 9 and before Step 8's disappearance check. Running before Step 8 keeps the jobs this step moved to `QUEUED` out of Step 8's `DISPATCHED` reconciliation.

    **Expected constraints:**

    - Detection granularity is the scan cycle interval (`DISPATCH_BUDGET_CHECK_INTERVAL_SEC`, default 10 seconds), a negligible error against a 30-minute threshold
    - Jobs whose image pull or PVC attach takes longer than the threshold are requeued. A requeue never loses the job, and the image layer cache remains on the node, so re-dispatch is fast. The backoff wait is still incurred
    - `WATCHER_DISPATCH_TIMEOUT_SEC` must be set well above the gap filling stall threshold (`GAP_FILLING_STALL_THRESHOLD_SEC`, default 300 seconds), so gap filling has time to attempt its own rescue (starting a large job through time-direction gap filling) before this last line of defence fires

**Relationship between the stall guard and gap filling:**

When this step requeues a stalled job to `QUEUED`, the job drops out of the Dispatcher's stall detection (`status = 'DISPATCHED'` with `dispatched_at` older than the threshold). Left as is, gap filling would not fire while the backoff is in effect, and small jobs in the same `(namespace, flavor)` would keep overtaking the large job (a regression of the starvation countermeasure). To prevent this, the Dispatcher's stall detection also treats jobs requeued by this step and waiting out their backoff (`status = 'QUEUED'` and `unschedulable_count > 0` and `retry_after > NOW()`) as stalled jobs (see [dispatcher.md](dispatcher.md) §2.4.2)

**Relationship between the grace period and scan cycle interval:**

`WATCHER_DISPATCH_GRACE_SEC` must be set to at least twice the Watcher's scan cycle interval (`DISPATCH_BUDGET_CHECK_INTERVAL_SEC`). This ensures that a K8s Job created by the Dispatcher mid-cycle is guaranteed to appear in the next cycle's K8s Job list. The current settings (grace 30 seconds vs cycle interval 10 seconds) provide a 3x safety margin.

**Relationship between `ttlSecondsAfterFinished` and scan cycle interval:**

`ttlSecondsAfterFinished` must be set sufficiently longer than the Watcher's scan cycle interval (currently sharing `DISPATCH_BUDGET_CHECK_INTERVAL_SEC`). If the TTL is too short, K8s Jobs that complete during a temporary Watcher stoppage (restart, failure, etc.) may be deleted by the TTL, causing Step 8 to record successfully completed jobs as FAILED. The current settings (TTL 300 seconds vs cycle interval 10 seconds) provide sufficient headroom even for Watcher restarts (typically 1–2 minutes). When changing the TTL or cycle interval, maintain this relationship.

**Terminal state regression guard:**

In Step 4's DB state update, jobs whose DB status is already `SUCCEEDED` or `FAILED` are not overwritten with `RUNNING`. Terminal states should never roll back to RUNNING in the normal flow; this defense-in-depth prevents inconsistent `finished_at` / `status` pairs in case Step 8's race (or similar) produces such a condition. A warning is logged whenever the regression is blocked.

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
- When K8s returns `Failed` (e.g. Pod exit code non-zero, startup failure): **FAILED**.

Time limit excess is enforced by the Watcher rather than by K8s `activeDeadlineSeconds` (§3 Step 9). Sweep jobs are handled the same way as regular jobs: a single time limit applies to the whole Job, measured from `started_at` (the time the first Pod entered RUNNING).

This ensures that sweeps with partially failed tasks are always treated as FAILED.

### 4.3 Transition to RUNNING

When the first Pod enters RUNNING (K8s Job's `status.active >= 1` and `status.ready >= 1`, same criterion as §3), the DB is updated to RUNNING. `started_at` and `node_name` are recorded, same as for regular jobs.

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
- `active`, `ready`, `succeeded`, `failed`, `completed_indexes`, `failed_indexes`

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
- **Step 10 DISPATCHED stall decision**: Targets are limited to DISPATCHED jobs whose K8s Job was observed, so the `(namespace, job_id) -> Job` map already loaded above is scanned directly. No dedicated DB query is issued.

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
| `WATCHER_DISPATCH_GRACE_SEC` | 30 | Grace period (seconds) before Step 8 marks a DISPATCHED job as FAILED due to a missing K8s Job. Until this much time has elapsed since `dispatched_at`, a missing K8s Job does not trigger the FAILED transition. Guards against the race between the Dispatcher and the Watcher's reconcile cycle. Recommended: at least 2x `DISPATCH_BUDGET_CHECK_INTERVAL_SEC` |
| `WATCHER_DISPATCH_TIMEOUT_SEC` | 1800 (30 min) | Stall tolerance (seconds) before Step 10 requeues a DISPATCHED job to QUEUED. A job that does not reach RUNNING within this much time after `dispatched_at` is treated as unschedulable. Set it well above `GAP_FILLING_STALL_THRESHOLD_SEC` (default 300 seconds) |
| `WATCHER_DISPATCH_BACKOFF_MAX_SEC` | 7200 (2 hours) | Ceiling (seconds) for Step 10's exponential backoff. However many requeues occur, `retry_after` is never set further ahead than this |

These settings are registered as standard keys in the `cjob-config` ConfigMap and can be updated via `cjobctl config set <key> <value>` (see [cjobctl.md](cjobctl.md) §`cjobctl config set`). After updating, apply the change with `cjobctl system restart watcher`.
