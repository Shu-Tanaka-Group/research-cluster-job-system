> *This document was auto-translated from the [Japanese original](../../docs/architecture/resources.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# Resource Design

This document describes the limits and settings related to compute resources (CPU, memory, GPU) and job counts. ConfigMap keys not directly related to resources, such as path settings and queue names, are out of scope and are covered in their respective design documents.

## 1. ResourceQuota

A safety net created in each user namespace.

The policy allows a single user to use all cores if resources are available, with fairness adjustments delegated to Kueue's BestEffortFIFO and the Dispatcher's DRF scheduling.
ResourceQuota functions not as an equal distribution mechanism, but as a safety net to prevent unintended unlimited consumption due to bugs or similar issues.

Configuration rationale:
- CPU / memory: Set slightly larger than the cluster total and rely on Kueue's admission control. Includes headroom for Job Pods (up to dispatch_limit) plus other compute resources the user is using (e.g., job submission Pods and data analysis Pods).
- Job count: Set taking into account dispatch_limit(32) and `ttlSecondsAfterFinished`(300 seconds = 5 minutes). Since SUCCEEDED/FAILED K8s Jobs are not explicitly deleted by the Watcher and remain until TTL expires, the quota is set with enough margin so that the sum of running jobs (max 32) and completed jobs within the TTL window does not exceed the ResourceQuota → 50. With the shortened TTL, the probability of reaching the quota in normal operation is extremely low. Because the sweep feature (capable of running hundreds to thousands of tasks in a single Job) is available, limiting the number of Jobs does not practically limit computational capacity.
- GPU: Set according to the total number of GPUs on GPU nodes. Setting to `"0"` prevents that user from running GPU jobs. Set to `"0"` or omit the GPU-related entries for users who do not use GPUs.

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: computing-quota
  namespace: user-alice
spec:
  hard:
    count/jobs.batch: "50"
    requests.cpu: "300"
    requests.memory: "1250Gi"
    limits.cpu: "300"
    limits.memory: "1250Gi"
    requests.nvidia.com/gpu: "4"
    limits.nvidia.com/gpu: "4"
```

## 2. Resource Limits Summary

### Limits on Job Count

| Limit | Location | Value | Manager | Scope | Target |
|---|---|---|---|---|---|
| `MAX_QUEUED_JOBS_PER_NAMESPACE` | ConfigMap | 500 | Submit API | Per user | Number of entries in the PostgreSQL `jobs` table (sum of QUEUED / DISPATCHING / DISPATCHED / HELD / CANCELLED). RUNNING is excluded because its upper bound is managed by `DISPATCH_BUDGET_PER_NAMESPACE`. |
| `DISPATCH_BUDGET_PER_NAMESPACE` | ConfigMap | 32 | Dispatcher | Per user × flavor | Limits the number of active jobs in the DB (sum of DISPATCHING + DISPATCHED + RUNNING) per `(namespace, flavor)` unit. If one flavor reaches its limit, dispatch continues for other flavors. |
| `DISPATCH_BATCH_SIZE` | ConfigMap | 50 | Dispatcher | Per cycle (global) | Maximum total number of jobs dispatched in a single dispatch cycle. Distributed fairly across namespaces using round-robin and DRF priority. |
| `DISPATCH_FETCH_MULTIPLIER` | ConfigMap | 10 | Dispatcher | Per cycle (global) | Multiplier for SQL candidate retrieval. Fetches `DISPATCH_BATCH_SIZE × DISPATCH_FETCH_MULTIPLIER` candidates in excess, then narrows down to `DISPATCH_BATCH_SIZE` after gap-filling and ResourceQuota filtering. Guarantees that even if all candidates from a DRF-prioritized namespace are filtered out, candidates from other namespaces are still dispatched. |
| `DISPATCH_ROUND_SIZE` | ConfigMap | 1 | Dispatcher | Per cycle (per namespace) | Controls the balance between round-robin and DRF. Smaller values make round-robin dominant (even distribution); setting to the same value as `DISPATCH_BUDGET_PER_NAMESPACE` makes DRF dominant (consumption-based priority control). See [dispatcher.md](dispatcher.md) §1.2 for tuning guidelines. |
| `DISPATCH_BUDGET_CHECK_INTERVAL_SEC` | ConfigMap | 10 | Dispatcher / Watcher | Global | Interval (in seconds) for the main loop of Dispatcher and Watcher. |
| `DISPATCH_RETRY_INTERVAL_SEC` | ConfigMap | 30 | Dispatcher | Per job | Wait time (in seconds) before retrying on temporary K8s API failures. |
| `DISPATCH_MAX_RETRIES` | ConfigMap | 5 | Dispatcher | Per job | Maximum retry count on temporary K8s API failures. When exceeded, the job transitions to FAILED. |
| `TTL_SECONDS_AFTER_FINISHED` | ConfigMap | 300 (5 min) | Dispatcher | Per job | Value set for the K8s Job's `ttlSecondsAfterFinished`. Completed K8s Jobs are automatically deleted after this many seconds. |
| `count/jobs.batch` | ResourceQuota | 50 | Kubernetes | Per user | Total number of `batch/v1 Job` objects on K8s. Counts the sum of running jobs and completed jobs awaiting TTL expiration. |

The four limits function as independent layers.

```
cjob add → DB registration (MAX_QUEUED_JOBS_PER_NAMESPACE: limit of 500)
              ↓
Dispatcher scans → dispatch_budget check (DISPATCH_BUDGET_PER_NAMESPACE: limit of 32 per flavor)
                 → fetch excess candidates (DISPATCH_BATCH_SIZE × DISPATCH_FETCH_MULTIPLIER)
                 → narrow down to DISPATCH_BATCH_SIZE after gap-filling and ResourceQuota filter (limit of 50 per cycle)
              ↓
K8s Job created → count/jobs.batch check (limit of 50)
```

Why `count/jobs.batch` is set to 50: Even when operating within the dispatch_budget limit, SUCCEEDED/FAILED K8s Jobs remain on K8s until TTL (5 minutes) expires, so the quota is set to absorb the total of running jobs and completed jobs awaiting TTL. With a TTL of 300 seconds (5 minutes), accumulation of TTL-waiting jobs is minimal for long- to medium-duration jobs, leaving ample headroom below 50. If short-lived jobs cycling rapidly temporarily reach the quota, it naturally recovers after TTL expiration, and the Dispatcher auto-recovers via retry.

**Relationship between `count/jobs.batch` and flavor-aware budget:** Since dispatch_budget is 32 per `(namespace, flavor)`, the theoretical maximum active jobs per namespace is `32 × number of flavors` (64 for 2 flavors), which may exceed `count/jobs.batch` (50). The Dispatcher pre-checks the remaining job count for `count/jobs.batch` using `hard_count` / `used_count` from the `namespace_resource_quotas` table to suppress dispatches that would exceed it (see [dispatcher.md](dispatcher.md) §2.5).

### Limits on CPU and Memory

| Limit | Location | Value | Manager | Scope | Target |
|---|---|---|---|---|---|
| ResourceQuota `requests.cpu` / `limits.cpu` | ResourceQuota (user namespace) | 300 | Kubernetes | Per user | Total upper limit of CPU that all Pods in the namespace (Job Pods + User Pods) can request and use. |
| ResourceQuota `requests.memory` / `limits.memory` | ResourceQuota (user namespace) | 1250Gi | Kubernetes | Per user | Total upper limit of memory that all Pods in the namespace can request and use. |
| ClusterQueue `nominalQuota` CPU | ClusterQueue | 256 | Kueue | Cluster-wide | Cluster-wide CPU limit that Kueue allocates to Job Pods. Shared across users. |
| ClusterQueue `nominalQuota` memory | ClusterQueue | 1000Gi | Kueue | Cluster-wide | Cluster-wide memory limit that Kueue allocates to Job Pods. Shared across users. |
| `CPU_LIMIT_BUFFER_MULTIPLIER` | ConfigMap | 1.0 | Dispatcher | Per job | Multiplier applied to the CPU limit. At `1.0`, request == limit (default). Setting to e.g. `1.05` sets the CPU limit to 1.05× the request, reducing CFS throttling from system processes. The request is not changed. |

Difference between ResourceQuota and ClusterQueue nominalQuota: ResourceQuota is an upper limit for all Pods in the namespace including User Pods (a safety net to prevent unlimited consumption due to bugs). ClusterQueue nominalQuota is the limit Kueue uses to decide Job Pod admission and controls the actual execution scheduling. User Pods do not go through Kueue and are not subject to ClusterQueue control.

### Settings Related to Gap Filling

| Setting | Location | Value | Manager | Scope | Description |
|---|---|---|---|---|---|
| `GAP_FILLING_ENABLED` | ConfigMap | true | Dispatcher | Global | Enables/disables the gap-filling logic. Setting to false reverts to legacy behavior. |
| `GAP_FILLING_STALL_THRESHOLD_SEC` | ConfigMap | 300 (5 min) | Dispatcher | Per job | Jobs where the elapsed time since DISPATCHED exceeds this value are considered stalled. |

See [dispatcher.md](dispatcher.md) §2.4 for details on gap filling.

### Settings Related to the DISPATCHED Stall Guard

| Setting | Location | Value | Owner | Scope | Description |
|---|---|---|---|---|---|
| `WATCHER_DISPATCH_TIMEOUT_SEC` | ConfigMap | 1800 (30 min) | Watcher | Per job | A job that has been DISPATCHED for longer than this without reaching RUNNING is treated as unschedulable: its K8s Job is deleted and the job is requeued to QUEUED. Set it well above `GAP_FILLING_STALL_THRESHOLD_SEC` (300 seconds). |
| `WATCHER_DISPATCH_BACKOFF_MAX_SEC` | ConfigMap | 7200 (2 hours) | Watcher | Per job | Ceiling for the exponential backoff written to `retry_after` on requeue. |

See [watcher.md](watcher.md) §3 Step 10 for details on the DISPATCHED stall guard.

### Settings Related to per-node bin-packing Pre-check

| Setting | Location | Value | Manager | Scope | Description |
|---|---|---|---|---|---|
| `NODE_BIN_PACKING_ENABLED` | ConfigMap | true | Dispatcher | Global | Enables/disables the per-node bin-packing pre-check. Setting to false skips the pre-check and reverts to the legacy behavior, where admission is decided solely by Kueue's flavor-level nominalQuota. |

See [dispatcher.md](dispatcher.md) §2.6 for details on the per-node bin-packing pre-check.

### Settings Related to Fair Sharing

| Setting | Location | Value | Manager | Scope | Description |
|---|---|---|---|---|---|
| `FAIR_SHARE_WINDOW_DAYS` | ConfigMap | 7 | Dispatcher | Global | Number of days for the sliding window used to aggregate consumption in DRF. Dominant share is calculated by summing the per-day consumption over the last N days. |
| `USAGE_RETENTION_DAYS` | ConfigMap | 7 | Dispatcher | Global | Retention period (in days) for `namespace_daily_usage`. Independent of `FAIR_SHARE_WINDOW_DAYS`; can be set to a longer period when consumption data is referenced for purposes other than DRF. |

The cluster-wide resource capacity used for DRF normalization is dynamically obtained via `SUM()` from the `node_resources` table ([database.md](database.md) §6). The former `CLUSTER_TOTAL_CPU_MILLICORES` / `CLUSTER_TOTAL_MEMORY_MIB` / `CLUSTER_TOTAL_GPUS` settings are deprecated. The CPU and memory in `node_resources` are the effective allocatable values with DaemonSet Pod requests subtracted (see [watcher.md](watcher.md) §1.1).

For details on per-day resource consumption, see [database.md](database.md) §5; for namespace weights, see [database.md](database.md) §4; for DRF scheduling details, see [dispatcher.md](dispatcher.md) §1.1 and §1.2.

### Settings Related to ResourceFlavor Definitions

| Setting | Location | Value | Manager | Scope | Description |
|---|---|---|---|---|---|
| `RESOURCE_FLAVORS` | ConfigMap | JSON array | Watcher / Submit API | Global | List of ResourceFlavor definitions. Each element has `name` (flavor name), `label_selector` (K8s node label selector), `gpu_resource_name` (GPU resource name, optional), and `image` (default container image, optional). Used by Watcher during node synchronization and by Submit API for flavor validation and image resolution. |
| `DEFAULT_FLAVOR` | ConfigMap | `cpu` | Submit API | Global | The default flavor name used when the user omits `--flavor`. Must match one of the flavor names in `RESOURCE_FLAVORS`. |
| `NODE_RESOURCE_SYNC_INTERVAL_SEC` | ConfigMap | 300 (5 min) | Watcher | Global | Node resource synchronization interval (in seconds). Executed once every N cycles of the Watcher's main loop. |
| `CLUSTER_QUEUE_NAME` | ConfigMap | `cjob-cluster-queue` | Watcher / cjobctl | Global | Name of the ClusterQueue. Used by Watcher during nominalQuota synchronization and by cjobctl in the `cluster` subcommands (`show-quota` / `set-quota` / `flavor-usage`). ClusterQueue is cluster-scoped and cannot be separated by namespace, so deployments that place several CJob instances on one cluster distinguish them here. |
| `RESOURCE_QUOTA_NAME` | ConfigMap | `computing-quota` | Watcher | Global | Name of the ResourceQuota object read from user namespaces. Used by Watcher during ResourceQuota synchronization. |
| `RESOURCE_QUOTA_SYNC_INTERVAL_SEC` | ConfigMap | 10 | Watcher | Global | ResourceQuota synchronization interval (in seconds). Executed once every N cycles of the Watcher's main loop. Operates on an interval independent of `NODE_RESOURCE_SYNC_INTERVAL_SEC`. |
| `USER_NAMESPACE_LABEL` | ConfigMap | `cjob.io/user-namespace=true` | Watcher | Global | Label selector for identifying user namespaces. Used by Watcher to retrieve the list of user namespaces during ResourceQuota synchronization. |

#### Example `RESOURCE_FLAVORS` Configuration

```json
[
  {"name": "cpu", "label_selector": "cjob.io/flavor=cpu"},
  {"name": "gpu-a100", "label_selector": "cjob.io/flavor=gpu-a100", "gpu_resource_name": "nvidia.com/gpu", "image": "your-registry/cjob-cuda:2.1.0"},
  {"name": "gpu-h100", "label_selector": "cjob.io/flavor=gpu-h100", "gpu_resource_name": "nvidia.com/gpu", "image": "your-registry/cjob-cuda:2.1.0"}
]
```

Meaning of each field:

| Field | Required | Description |
|---|---|---|
| `name` | Required | Flavor name. Must match the Kueue ResourceFlavor name and the `jobs.flavor` / `node_resources.flavor` values in the DB. |
| `label_selector` | Required | K8s node label selector. Uses the common key `cjob.io/flavor` across all flavors, with the flavor name as the value. Must match the `nodeLabels` of the Kueue ResourceFlavor. |
| `gpu_resource_name` | Optional | K8s resource name for the GPU resource (e.g., `nvidia.com/gpu`, `amd.com/gpu`). If omitted, the flavor is treated as a non-GPU flavor and jobs with `gpu > 0` are rejected. |
| `image` | Optional | Default container image for jobs run on that flavor. If omitted, the submitting User Pod's image is used (see the image resolution order in [api.md](api.md) §2.2). When specified, it must satisfy the prerequisites ([prerequisites.md](prerequisites.md) §2.1) and the Kyverno allowed pattern ([deployment.md](../deployment.md) §14). |

The `name` of a flavor must match the `metadata.name` of the Kueue ResourceFlavor. This unifies the name specified with `cjobctl cluster set-quota --flavor <name>` and the flavor value in the DB, eliminating the need for conversion.

#### Schema Constraints for `RESOURCE_FLAVORS`

The table in this section is authoritative for the `RESOURCE_FLAVORS` schema. Both `cjobctl config set` (Rust, [cjobctl.md](cjobctl.md) §5.5) and the server-side `FlavorDefinition` (Python) follow this definition. When changing the schema, update this section first and keep both implementations in sync.

| # | Constraint | Verified by |
|---|---|---|
| 1 | The top level must be a JSON array with at least one element | `cjobctl config set` / server startup (array only) |
| 2 | Each element must be a JSON object | `cjobctl config set` / server startup |
| 3 | `name` / `label_selector` must be present, be strings, and not be empty | `cjobctl config set` / server startup (presence and type only) |
| 4 | If `gpu_resource_name` is specified, it must be a string and not be empty (`null` is equivalent to omission) | `cjobctl config set` / server startup (type only) |
| 5 | If `image` is specified, it must be a string and not be empty (`null` is equivalent to omission) | `cjobctl config set` / server startup (type only) |
| 6 | No field outside the table above may be present | `cjobctl config set` / server startup |
| 7 | `name` must not be duplicated | `cjobctl config set` |
| 8 | `label_selector` must be in `key=value` form (exactly one `=`, both sides non-empty) | `cjobctl config set` |
| 9 | `DEFAULT_FLAVOR` must match one of the `name` values | `cjobctl config set` (both when setting `RESOURCE_FLAVORS` and when setting `DEFAULT_FLAVOR`) |

**Rejection of unknown fields**: The server-side `FlavorDefinition` sets `extra="forbid"`, so a definition containing a field outside the table above makes Submit API / Dispatcher / Watcher fail to start. This prevents the accident where a typo in an optional field such as `gpu_resouce_name` is silently ignored and a GPU flavor is treated as a non-GPU flavor.

Constraints 7, 8, and 9 are not detected at startup (they cannot be expressed by the pydantic model alone), so validation in `cjobctl config set` is the only line of defense. Note that editing the ConfigMap directly with `kubectl` instead of going through `cjobctl` bypasses these checks.

For details on node resource synchronization, see [watcher.md](watcher.md) §1.1; for DB table definitions, see [database.md](database.md) §6.

### Limits Related to Sweep

| Limit | Location | Value | Manager | Scope | Target |
|---|---|---|---|---|---|
| `MAX_SWEEP_COMPLETIONS` | ConfigMap | 1000 | Submit API | Per sweep job | Upper limit for `completions` (number of tasks). |

### Limits Related to Execution Time

| Limit | Location | Value | Manager | Scope | Target |
|---|---|---|---|---|---|
| `DEFAULT_TIME_LIMIT_SECONDS` | ConfigMap | 86400 (24h) | Submit API | Per job | Default execution time limit applied when `time_limit_seconds` is omitted. |
| `MAX_TIME_LIMIT_SECONDS` | ConfigMap | 604800 (7d) | Submit API | Per job | Maximum value of `time_limit_seconds` that a user can specify. |
| Time limit enforcement | Watcher (reconcile cycle) | DB's `time_limit_seconds` | Watcher | Per job | Execution time limit measured from the job's `started_at` (the time the Watcher observed RUNNING). When the limit is exceeded, the Watcher deletes the K8s Job and transitions it to FAILED (`time limit exceeded`) (see [watcher.md](watcher.md) §3 Step 9). Neither the Kueue admission wait time (suspend period) nor the time a Pod spends Pending waiting for a node after admission is included. |
