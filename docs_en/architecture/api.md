> *This document was auto-translated from the [Japanese original](../../docs/architecture/api.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# API Design

The CLI is implemented as a thin client that calls this API.
All endpoints perform authentication and authorization via ServiceAccount JWT (see [auth_policy.md](../auth_policy.md) for details).

## 1. Common Error Response Specification

The following errors may occur across all endpoints.

| HTTP Status | Trigger Condition | Response Body Example |
|---|---|---|
| 401 | JWT is invalid, expired, or missing | `{ "detail": "Unauthorized" }` |
| 403 | The namespace has no CJob user configuration (`cjob.io/username` annotation) | `{ "detail": "Namespace is not configured as a CJob user namespace" }` |
| 404 | Non-existent job_id, or another user's job_id | `{ "detail": "Job not found" }` |
| 409 | Reset in progress (submitting to a namespace that has a `DELETING` job) | `{ "detail": "Cannot submit jobs while reset is in progress. Please wait and try again." }` |
| 500 | Internal error such as namespace read failure | `{ "detail": "Internal server error" }` |
| 503 | Temporary service unavailability such as DB write failure | `{ "detail": "Service temporarily unavailable" }` |

**404 policy**: Accessing another user's job also returns 404. By hiding the existence of jobs, information leakage is prevented.

**401 policy**: Returned when TokenReview fails (invalid or expired JWT). The response body is a fixed string and does not include detailed error causes.

**403 policy**: Returned when JWT authentication succeeds but the namespace lacks the `cjob.io/username` annotation and cannot be recognized as a CJob user.

**Rate limiting policy**: Since the Submit API calls the K8s TokenReview API for each request, large numbers of requests can put load on the K8s API server. However, the Submit API's own CPU/memory limits (500m / 512Mi) effectively serve as throughput caps, so explicit rate limiting is deemed unnecessary at the current scale (approximately 10 users). If the number of users grows to several dozen or more, consider per-namespace rate limiting using `slowapi` or similar.

## 2. POST /v1/jobs

Submit a single job.

### request

```json
{
  "command": "python main.py --alpha 0.1 --beta 16",
  "image": "your-registry/cjob-jupyter:2.1.0",
  "cwd": "/home/jovyan/project-a/exp1",
  "env": {
    "OMP_NUM_THREADS": "4",
    "PYTHONPATH": "/home/jovyan/project-a"
  },
  "resources": {
    "cpu": "2",
    "memory": "4Gi",
    "gpu": 0,
    "flavor": "cpu"
  },
  "time_limit_seconds": 3600
}
```

Each field within `resources` is optional. Default values when omitted:

| Field | Default | Description |
|---|---|---|
| `cpu` | `"1"` | CPU resource |
| `memory` | `"1Gi"` | Memory resource |
| `gpu` | `0` | Number of GPUs |
| `flavor` | Server-side default (ConfigMap: `DEFAULT_FLAVOR`, default `cpu`) | ResourceFlavor name |

### response (201 Created)

```json
{
  "job_id": 1,
  "status": "QUEUED"
}
```

### Validation

If `resources.flavor` does not exist in `RESOURCE_FLAVORS`, returns 400.

```json
{ "detail": "The specified flavor 'xxx' does not exist. Available flavors: cpu, gpu-a100" }
```

If the requested resources (CPU / memory) exceed the effective limit for the specified flavor, returns 400.
The effective limit is determined by `min(max node allocatable, nominalQuota)`.
This prevents jobs that cannot fit on a single node or exceed the quota from being accepted by Kueue and stalling indefinitely in the DISPATCHED state.
If no node for the specified flavor exists in the `node_resources` table (e.g., Watcher not running), this validation is skipped.
If the `flavor_quotas` table has no data, validation is performed using only the max node allocatable.

```json
{ "detail": "Requested CPU (128) exceeds the maximum node capacity (64000m) for flavor 'cpu'" }
```

```json
{ "detail": "Requested memory (2Ti) exceeds the maximum node capacity (262144Mi) for flavor 'cpu'" }
```

```json
{ "detail": "Requested CPU (128) exceeds the quota (64000m) for flavor 'cpu'" }
```

```json
{ "detail": "Requested memory (2Ti) exceeds the quota (262144Mi) for flavor 'cpu'" }
```

If `resources.gpu > 0` and the specified flavor has no `gpu_resource_name` configured (a non-GPU flavor), returns 400. If no GPU nodes are registered, returns 400. If the requested GPU exceeds the effective limit (`min(max node GPU, nominalQuota GPU)`) for the flavor, also returns 400.

```json
{ "detail": "flavor 'cpu' does not support GPU" }
```

```json
{ "detail": "No GPU nodes registered for flavor 'gpu-a100'" }
```

```json
{ "detail": "Requested GPU (8) exceeds the maximum node capacity (4) for flavor 'gpu-a100'" }
```

```json
{ "detail": "Requested GPU (8) exceeds the quota (4) for flavor 'gpu-a100'" }
```

`time_limit_seconds` is optional. When omitted, the server-side default is used (ConfigMap: `DEFAULT_TIME_LIMIT_SECONDS`, default 86400 = 24 hours).
If a value exceeding `MAX_TIME_LIMIT_SECONDS` (default 604800 = 7 days) is specified, returns 400. If a value of 0 or less is specified, also returns 400.

```json
{ "detail": "time_limit_seconds must be 604800 seconds (7 days) or less" }
```

```json
{ "detail": "time_limit_seconds must be 1 or greater" }
```

If `command` is an empty string, returns 400.

```json
{ "detail": "command cannot be empty" }
```

If `image` is an empty string, returns 400.

```json
{ "detail": "image cannot be empty" }
```

If the total number of jobs in the namespace (sum of QUEUED / DISPATCHING / DISPATCHED / HELD / CANCELLED) reaches
`MAX_QUEUED_JOBS_PER_NAMESPACE` (default 500), returns 429.
RUNNING is managed by a separate `DISPATCH_BUDGET_PER_NAMESPACE` limit (per flavor) and does not accumulate without bound, so it is excluded from the count. DISPATCHING / DISPATCHED are also limited by the budget, but since they are transient states with short dwell times, they are currently included in the count.
Including CANCELLED jobs prevents DB bloat from unlimited cancel → resubmit cycles.
When the limit is reached, use `cjob delete` to remove CANCELLED jobs before resubmitting.

```json
{ "detail": "The maximum number of submittable jobs (500) has been reached" }
```

If the namespace has even one job in the `DELETING` state, returns 409 (reset in progress).

```json
{ "detail": "Cannot submit jobs while reset is in progress. Please wait and try again." }
```

## 2.1 POST /v1/sweep

Submit a single parameter sweep job. Executed as a K8s Indexed Job, with each task identified by `$CJOB_INDEX` (0-origin).

`parallelism` is optional, defaulting to 1.

The `_INDEX_` placeholder accepted by the CLI is replaced with `$CJOB_INDEX` on the CLI client side before being sent to this API (see [cli.md](cli.md) §3 and [dispatcher.md](dispatcher.md) §3.3.1). Therefore, this API always receives requests with the command already substituted in `$CJOB_INDEX` format.

### request

```json
{
  "command": "python main.py --trial $CJOB_INDEX",
  "image": "your-registry/cjob-jupyter:2.1.0",
  "cwd": "/home/jovyan/project-a",
  "env": {
    "OMP_NUM_THREADS": "4"
  },
  "resources": {
    "cpu": "2",
    "memory": "4Gi",
    "gpu": 0,
    "flavor": "cpu"
  },
  "completions": 100,
  "parallelism": 10,
  "time_limit_seconds": 21600
}
```

### response (201 Created)

```json
{
  "job_id": 3,
  "status": "QUEUED"
}
```

### Validation

In addition to the validation shared with `POST /v1/jobs` (single node resource excess, GPU validation, time_limit, job count limit, DELETING check), the following sweep-specific validation is performed.

- `completions` is less than 1 → 400
- `completions` exceeds `MAX_SWEEP_COMPLETIONS` (default 1000) → 400
- `parallelism` is less than 1 → 400
- `parallelism` exceeds `completions` → 400
- `parallelism × Pod resources` exceeds the effective limit for the specified flavor (`min(total allocatable, nominalQuota)`) → 400

```json
{ "detail": "completions must be between 1 and 1000" }
```

```json
{ "detail": "parallelism must be between 1 and completions" }
```

```json
{ "detail": "parallelism × requested CPU (20000m) exceeds the total CPU (256000m) for flavor 'cpu'" }
```

```json
{ "detail": "parallelism × requested GPU (8) exceeds the total GPU (4) for flavor 'gpu-a100'" }
```

## 3. GET /v1/jobs

Retrieve the job list. Only returns jobs belonging to the namespace of the JWT.

### Query Parameters

| Parameter | Type | Behavior when omitted |
|---|---|---|
| `status` | string (optional) | Returns all statuses |
| `flavor` | string (optional) | Returns all flavors |
| `time_limit_ge` | integer (optional, seconds) | No filtering |
| `time_limit_lt` | integer (optional, seconds) | No filtering |
| `limit` | integer (optional) | Returns all records (note: the CLI sends `limit=50` by default; when using the API directly, specifying an appropriate `limit` is recommended) |
| `order` | string (`"asc"` or `"desc"`) | `"asc"` (ascending by JOB_ID) |

`time_limit_ge` / `time_limit_lt` are range filters for `time_limit_seconds`. `time_limit_ge` means "greater than or equal to" and `time_limit_lt` means "less than". When both are specified, they are combined with AND.

When `limit` is specified, the latest N records (highest JOB_ID) are always selected and returned sorted according to `order`.

```
GET /v1/jobs
GET /v1/jobs?status=RUNNING
GET /v1/jobs?status=FAILED&limit=10
GET /v1/jobs?limit=50&order=desc
GET /v1/jobs?flavor=gpu-a100
GET /v1/jobs?status=QUEUED&time_limit_ge=21600
GET /v1/jobs?time_limit_lt=43200
GET /v1/jobs?time_limit_ge=21600&time_limit_lt=43200
```

### response

```json
{
  "jobs": [
    {
      "job_id": 1,
      "status": "RUNNING",
      "flavor": "cpu",
      "command": "python main.py --alpha 0.1 --beta 16",
      "created_at": "2026-03-23T12:34:56Z",
      "finished_at": null,
      "time_limit_seconds": 86400,
      "completions": null,
      "parallelism": null,
      "succeeded_count": null,
      "failed_count": null
    }
  ],
  "total_count": 1,
  "log_base_dir": "/home/jovyan/.cjob/logs"
}
```

`total_count` returns the total number of jobs matching the filter conditions (before applying `limit`).

`log_base_dir` returns the log base directory (`LOG_BASE_DIR`) configured in the server-side ConfigMap. The CLI uses this value for bulk log deletion during reset.

## 4. GET /v1/jobs/{job_id}

Retrieve the details of an individual job.

### response

```json
{
  "job_id": 1,
  "status": "SUCCEEDED",
  "namespace": "user-alice",
  "command": "python main.py --alpha 0.1 --beta 16",
  "cwd": "/home/jovyan/project-a/exp1",
  "cpu": "2",
  "memory": "4Gi",
  "gpu": 0,
  "flavor": "cpu",
  "time_limit_seconds": 86400,
  "k8s_job_name": "cjob-alice-1",
  "log_dir": "/home/jovyan/.cjob/logs/1",
  "created_at": "2026-03-23T12:34:56Z",
  "dispatched_at": "2026-03-23T12:35:02Z",
  "started_at": "2026-03-23T12:35:10Z",
  "finished_at": "2026-03-23T12:37:10Z",
  "last_error": null,
  "completions": null,
  "parallelism": null,
  "succeeded_count": null,
  "failed_count": null,
  "completed_indexes": null,
  "failed_indexes": null,
  "node_name": ["worker07", "worker08"]
}
```

`node_name` is a list of node names used for job execution (`list[str] | null`). The Watcher cumulatively records these when transitioning to RUNNING and when sweep progress changes. For unstarted jobs such as QUEUED / DISPATCHED, this is `null`. For normal jobs, it is a single-element list; for sweep jobs, it is a list of all nodes used.

### Error Response

If the job_id does not exist or belongs to another user, returns 404.

```json
{ "detail": "Job not found" }
```

## 5. POST /v1/jobs/{job_id}/cancel

Cancel a job.

| State | API processing |
|---|---|
| `QUEUED` | Updates DB to `CANCELLED`. The Dispatcher skips it if `CANCELLED` on the next scan |
| `HELD` | Updates DB to `CANCELLED`. No K8s Job deletion needed as none was created |
| `DISPATCHING` | Updates DB to `CANCELLED`. If cancelled before the CAS update, the Dispatcher skips it. If cancelled after the CAS update, the K8s Job is created but the Watcher deletes it during periodic monitoring (same path as `DISPATCHED` / `RUNNING`) |
| `DISPATCHED` / `RUNNING` | Updates DB to `CANCELLED`. The Watcher deletes the K8s Job for `CANCELLED` jobs during periodic monitoring |
| `SUCCEEDED` / `FAILED` / `CANCELLED` | No change needed. Returned as `skipped` |
| `DELETING` | Reset in progress, no change needed. Returned as `skipped` |

After deleting the K8s Job, the Watcher confirms that the DB status is `CANCELLED` and maintains that state (does not transition to `FAILED`).

### response

```json
{
  "job_id": 1,
  "status": "CANCELLED"
}
```

### Error Response

If the job_id does not exist or belongs to another user, returns 404.

```json
{ "detail": "Job not found" }
```

## 6. POST /v1/jobs/cancel

Cancel multiple jobs at once. Range specifications and individual multiple specifications are expanded on the CLI side before sending.

### request

```json
{
  "job_ids": [1, 2, 3, 4, 5]
}
```

### response

```json
{
  "cancelled":  [1, 2, 3],
  "skipped":    [4, 5],
  "not_found":  []
}
```

`skipped` applies when the target job is already in SUCCEEDED / FAILED / CANCELLED / DELETING state.

## 7. POST /v1/jobs/{job_id}/hold

Put a job on hold. Jobs on hold are excluded from Dispatcher dispatch targets.

| State | API processing |
|---|---|
| `QUEUED` | Updates DB to `HELD`. Since the Dispatcher only retrieves jobs with `status = 'QUEUED'`, `HELD` jobs are automatically skipped |
| `DISPATCHING` / `DISPATCHED` / `RUNNING` | Already in dispatch processing or running, cannot be held. Returned as `skipped` |
| `HELD` | Already on hold. Returned as `skipped` |
| `SUCCEEDED` / `FAILED` / `CANCELLED` | Already completed. Returned as `skipped` |
| `DELETING` | Reset in progress. Returned as `skipped` |

### response

```json
{
  "job_id": 1,
  "status": "HELD"
}
```

### Error Response

If the job_id does not exist or belongs to another user, returns 404.

```json
{ "detail": "Job not found" }
```

## 8. POST /v1/jobs/hold

Put multiple jobs on hold at once. Range specifications and individual multiple specifications are expanded on the CLI side before sending.

When `job_ids` is omitted (equivalent to `--all`), all `QUEUED` jobs in the namespace are targeted for hold.

### request (individual specification)

```json
{
  "job_ids": [1, 2, 3, 4, 5]
}
```

### request (hold all)

```json
{}
```

### response

```json
{
  "held":      [1, 2, 3],
  "skipped":   [4, 5],
  "not_found": []
}
```

`skipped` applies when the target job is in a state other than QUEUED (HELD / DISPATCHING / DISPATCHED / RUNNING / SUCCEEDED / FAILED / CANCELLED / DELETING).

## 9. POST /v1/jobs/{job_id}/release

Return a held job to the queue.

| State | API processing |
|---|---|
| `HELD` | Updates DB to `QUEUED`. The Dispatcher selects it as a dispatch target on the next scan |
| `QUEUED` / `DISPATCHING` / `DISPATCHED` / `RUNNING` | Not on hold. Returned as `skipped` |
| `SUCCEEDED` / `FAILED` / `CANCELLED` | Already completed. Returned as `skipped` |
| `DELETING` | Reset in progress. Returned as `skipped` |

### response

```json
{
  "job_id": 1,
  "status": "QUEUED"
}
```

### Error Response

If the job_id does not exist or belongs to another user, returns 404.

```json
{ "detail": "Job not found" }
```

## 10. POST /v1/jobs/release

Release holds on multiple jobs at once. Range specifications and individual multiple specifications are expanded on the CLI side before sending.

When `job_ids` is omitted (equivalent to `--all`), all `HELD` jobs in the namespace are targeted for release.

### request (individual specification)

```json
{
  "job_ids": [1, 2, 3, 4, 5]
}
```

### request (release all)

```json
{}
```

### response

```json
{
  "released":  [1, 2, 3],
  "skipped":   [4, 5],
  "not_found": []
}
```

`skipped` applies when the target job is in a state other than HELD.

## 11. POST /v1/jobs/{job_id}/set

Modify parameters of a QUEUED / HELD job. Only the specified fields are updated; unspecified fields retain their current values.

| State | API processing |
|---|---|
| `QUEUED` | Updates the specified parameters. The Dispatcher dispatches with the updated values on the next scan |
| `HELD` | Updates the specified parameters. Dispatched with the updated values after release |
| `DISPATCHING` / `DISPATCHED` / `RUNNING` | K8s Job already created, cannot be modified. Returned as `skipped` |
| `SUCCEEDED` / `FAILED` / `CANCELLED` | Already completed. Returned as `skipped` |
| `DELETING` | Reset in progress. Returned as `skipped` |

### request

```json
{
  "flavor": "cpu-sub",
  "cpu": "4",
  "memory": "16Gi",
  "time_limit_seconds": 43200
}
```

All fields are optional. However, at least one field must be specified. If all fields are omitted, returns 400.

| Field | Type | Description |
|---|---|---|
| `cpu` | `string \| null` | CPU resource (e.g., `"2"`, `"500m"`) |
| `memory` | `string \| null` | Memory resource (e.g., `"4Gi"`, `"500Mi"`) |
| `gpu` | `int \| null` | Number of GPUs |
| `flavor` | `string \| null` | ResourceFlavor name |
| `time_limit_seconds` | `int \| null` | Execution time limit (seconds) |

### response

```json
{
  "job_id": 1,
  "status": "QUEUED"
}
```

`status` is the current status of the job. On successful modification, returns `QUEUED` or `HELD`; when `skipped`, returns the actual status.

### Validation

Validation is performed against the merged state of the specified fields and the current values of unspecified fields. Validation rules are the same as §2 (POST /v1/jobs): flavor existence check, CPU/memory limits, GPU compatibility, time_limit range.

```json
{ "detail": "Please specify at least one parameter to modify" }
```

### Error Response

If the job_id does not exist or belongs to another user, returns 404.

```json
{ "detail": "Job not found" }
```

### Race Condition Prevention

To prevent conflicts with the Dispatcher's CAS (`UPDATE ... WHERE status = 'QUEUED'`), parameter updates are performed with a conditional UPDATE using `WHERE status IN ('QUEUED', 'HELD')`. If the status has changed (rowcount = 0), returns as `skipped`.

## 12. POST /v1/jobs/set

Modify parameters of multiple jobs at once. The same change is applied to all jobs. Range specifications and individual multiple specifications are expanded on the CLI side before sending.

### request

```json
{
  "job_ids": [1, 2, 3],
  "flavor": "cpu-sub",
  "cpu": "4"
}
```

### response

```json
{
  "modified":  [1, 2],
  "skipped":   [3],
  "not_found": []
}
```

`skipped` applies when the target job is in a state other than QUEUED / HELD. Validation errors occur independently for each job, so some jobs may succeed while others fail. However, since the same parameters are applied to all jobs, if a validation error occurs, it typically occurs for all jobs.

## 13. POST /v1/jobs/delete

Delete completed jobs. Range specifications and individual multiple specifications are expanded on the CLI side before sending.

Only jobs in CANCELLED / SUCCEEDED / FAILED state are eligible for deletion.
Jobs in QUEUED / DISPATCHING / DISPATCHED / RUNNING / HELD state are not deleted and returned as `skipped`.
Jobs in DELETING state are not deleted and returned as `skipped` because Watcher reset cleanup is in progress.

When `job_ids` is omitted (equivalent to `--all`), all completed jobs in the namespace are targeted for deletion.
Counters are not reset.

### request (individual specification)

```json
{
  "job_ids": [1, 2, 3]
}
```

### request (delete all)

```json
{}
```

### response

```json
{
  "deleted":   [1, 2],
  "log_dirs":  ["/home/jovyan/.cjob/logs/1", "/home/jovyan/.cjob/logs/2"],
  "skipped":   [
    { "job_id": 3, "reason": "running" },
    { "job_id": 4, "reason": "deleting" }
  ],
  "not_found": []
}
```

`log_dirs` returns the list of `log_dir` values for deleted jobs. The CLI uses these values to delete log directories on the PVC. They correspond in the same order as `deleted`.

`skipped` applies when the target job is in QUEUED / DISPATCHING / DISPATCHED / RUNNING / HELD / DELETING state.
The `reason` value is one of three: `"running"` (QUEUED / DISPATCHING / DISPATCHED / RUNNING), `"held"` (HELD), or `"deleting"` (DELETING).
The CLI branches its message based on `reason`. For QUEUED / DISPATCHING / DISPATCHED / RUNNING, it prompts the user to run `cjob cancel` first; for HELD, it prompts `cjob release` or `cjob cancel`; for DELETING, it displays that a reset is in progress (see [cli.md](cli.md) §10).

**Design difference from §6 (cancel):**
The `skipped` in §6 has only a single meaning of "already finished or processed," so a flat list of job_ids is sufficient. In contrast, the `skipped` in §11 fundamentally differs in the action the CLI should take: "running (should prompt cancel)", "on hold (should prompt release or cancel)", and "DELETING (nothing can be done)". Furthermore, having the CLI first check the state via `GET /v1/jobs/{job_id}` and then branch introduces a race condition, so that approach is not adopted. Including `reason` in the response allows the skip decision and reason retrieval to be done atomically.

## 14. POST /v1/reset

Reset all job history for the user and return job_id numbering to 1.

Reset conditions: All jobs must be in CANCELLED / SUCCEEDED / FAILED state.
Returns 409 if any of the following apply:

- Any jobs in QUEUED / DISPATCHING / DISPATCHED / RUNNING / HELD state exist (incomplete jobs present)
- Any jobs in DELETING state exist (previous reset processing not yet complete)

If conditions are met, the Submit API changes the status of all jobs to `DELETING` and returns immediately.
Actual K8s Job deletion, DB record deletion, and counter reset are performed asynchronously by the Watcher.
Therefore, when the response is returned, the reset is not yet complete.

The job_id counter reset (`next_id = 1`) is performed by the Watcher after all `DELETING` records have been processed.
This ensures that even if a new job is submitted before reset completion, job_id=1 is not issued, preventing K8s Job name collisions.

### response (success · 202 Accepted)

```json
{
  "status": "accepted"
}
```

### response (running jobs exist · 409)

```json
{
  "message": "Cannot reset because there are incomplete jobs",
  "blocking_job_ids": [3, 7, 12]
}
```

### response (reset in progress · 409)

```json
{
  "message": "Cannot re-run reset because a reset is in progress. Please wait and try again."
}
```

## 15. GET /v1/usage

Retrieve resource usage for your own namespace for the last `FAIR_SHARE_WINDOW_DAYS` days. Aggregates and returns daily consumption from the `namespace_daily_usage` table.

### response

```json
{
  "window_days": 7,
  "daily": [
    {
      "date": "2026-03-23",
      "cpu_millicores_seconds": 86400000,
      "memory_mib_seconds": 176947200,
      "gpu_seconds": 0
    },
    {
      "date": "2026-03-24",
      "cpu_millicores_seconds": 45000000,
      "memory_mib_seconds": 92160000,
      "gpu_seconds": 0
    }
  ],
  "total_cpu_millicores_seconds": 131400000,
  "total_memory_mib_seconds": 269107200,
  "total_gpu_seconds": 0
}
```

`daily` is sorted in ascending order of `usage_date`. If there is no usage within the window, `daily` is an empty array and each total is 0.

### resource_quota

Retrieves ResourceQuota information for your namespace from the `namespace_resource_quotas` table and returns it as the `resource_quota` field. Returns `null` if no ResourceQuota is configured or if the Watcher has not yet synced and there is no row in the table.

`hard_count` / `used_count` correspond to `count/jobs.batch` in the K8s ResourceQuota. If the ResourceQuota does not include `count/jobs.batch`, these are `null`.

```json
{
  "window_days": 7,
  "daily": [...],
  "total_cpu_millicores_seconds": 131400000,
  "total_memory_mib_seconds": 269107200,
  "total_gpu_seconds": 0,
  "resource_quota": {
    "hard_cpu_millicores": 300000,
    "hard_memory_mib": 1280000,
    "hard_gpu": 4,
    "hard_count": 50,
    "used_cpu_millicores": 280000,
    "used_memory_mib": 819200,
    "used_gpu": 1,
    "used_count": 12
  }
}
```

When ResourceQuota does not exist:

```json
{
  "window_days": 7,
  "daily": [...],
  "total_cpu_millicores_seconds": 0,
  "total_memory_mib_seconds": 0,
  "total_gpu_seconds": 0,
  "resource_quota": null
}
```

## 16. GET /v1/flavors

Returns the list of available ResourceFlavors and their resource information. No authentication required.

### response

```json
{
  "flavors": [
    {
      "name": "cpu",
      "has_gpu": false,
      "nodes": [
        {"node_name": "worker07", "cpu_millicores": 128000, "memory_mib": 515481, "gpu": 0},
        {"node_name": "worker08", "cpu_millicores": 128000, "memory_mib": 515481, "gpu": 0}
      ],
      "quota": {"cpu": "256", "memory": "1000Gi", "gpu": "0"}
    },
    {
      "name": "gpu-a100",
      "has_gpu": true,
      "nodes": [
        {"node_name": "gworker02", "cpu_millicores": 128000, "memory_mib": 515686, "gpu": 4}
      ],
      "quota": {"cpu": "64", "memory": "500Gi", "gpu": "4"}
    }
  ],
  "default_flavor": "cpu"
}
```

The `nodes` for each flavor contains the list of nodes belonging to that flavor from the `node_resources` table. Flavors without node information (Watcher not running) have `nodes` as an empty array.

`quota` contains the ClusterQueue nominalQuota retrieved from the `flavor_quotas` table. Flavors without quota information (Watcher not yet synced) have `quota` as `null`.

`default_flavor` is the value of the ConfigMap `DEFAULT_FLAVOR`.

## 17. GET /v1/cli/version

Returns the latest version of the CLI binary placed on the PVC. No authentication required.

The Submit API reads the `latest` file on the PVC (`cli-binary`) and returns the latest version string.

### response

```json
{
  "version": "1.2.0"
}
```

### Error Response

If the `latest` file does not exist on the PVC (binary not deployed), returns 404.

```json
{ "detail": "CLI binary not found" }
```

## 18. GET /v1/cli/versions

Returns the complete list of all CLI binary versions placed on the PVC. No authentication required.

The Submit API scans directory entries on the PVC (`cli-binary`) and returns the list of available versions. The `latest` file and entries other than directories are excluded. Versions are sorted in descending semver order (parsed with `packaging.version.Version`; entries that cannot be parsed are excluded).

### response

```json
{
  "versions": ["1.3.1-beta.2", "1.3.1-beta.1", "1.3.0", "1.2.0", "1.1.0"],
  "latest": "1.3.0"
}
```

### Error Response

If the `latest` file does not exist on the PVC, returns 404.

```json
{ "detail": "CLI binary not found" }
```

## 19. GET /v1/cli/download

Returns the CLI binary placed on the PVC. No authentication required.

### Query Parameters

| Parameter | Type | Behavior when omitted |
|---|---|---|
| `version` | string (optional) | Returns the binary for the version indicated by the `latest` file |

When `version` is specified, returns the `<version>/cjob` binary for that version. When omitted, reads the version from the `latest` file (for backward compatibility).

Returns with `Content-Type: application/octet-stream`.

```
GET /v1/cli/download
GET /v1/cli/download?version=1.3.1-beta.1
```

### Error Response

If the version string format is invalid, returns 400.

```json
{ "detail": "Invalid version format" }
```

If the binary does not exist, returns 404.

```json
{ "detail": "CLI binary not found" }
```
