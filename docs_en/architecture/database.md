> *This document was auto-translated from the [Japanese original](../../docs/architecture/database.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# PostgreSQL Design

## 1. `jobs` Table

job_id is a sequential number starting from 1 per user (namespace).
Global uniqueness is guaranteed by the composite primary key `(namespace, job_id)`.

```sql
CREATE TABLE jobs (
    job_id        INTEGER NOT NULL,
    "user"        TEXT NOT NULL,
    namespace     TEXT NOT NULL,
    image         TEXT NOT NULL,           -- CLI fetches from CJOB_IMAGE env var (falls back to JUPYTER_IMAGE if not set)
    command       TEXT NOT NULL,
    cwd           TEXT NOT NULL,
    env_json      JSONB NOT NULL DEFAULT '{}',
    cpu           TEXT NOT NULL,
    memory        TEXT NOT NULL,
    gpu           INTEGER NOT NULL DEFAULT 0,
    flavor        TEXT NOT NULL DEFAULT 'cpu', -- ResourceFlavor name for job execution destination (e.g., 'cpu', 'gpu-a100')
    time_limit_seconds INTEGER NOT NULL,   -- Execution time limit (seconds). Set as K8s Job activeDeadlineSeconds
    status        TEXT NOT NULL,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    retry_after   TIMESTAMPTZ,              -- Retry unlock time for K8s transient failures (NULL = eligible immediately)
    k8s_job_name  TEXT,
    log_dir       TEXT,          -- /home/jovyan/.cjob/logs/<job_id>
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dispatched_at TIMESTAMPTZ,
    started_at    TIMESTAMPTZ,             -- Time when Pod transitioned to RUNNING (recorded by Watcher)
    finished_at   TIMESTAMPTZ,
    last_error    TEXT,
    completions       INTEGER,       -- Total task count for sweep. NULL = regular job
    parallelism       INTEGER,       -- Concurrent execution count for sweep
    completed_indexes TEXT,          -- Successful indexes (K8s compressed notation, e.g., "0-49,51-99")
    failed_indexes    TEXT,          -- Failed indexes (K8s compressed notation, e.g., "50")
    succeeded_count   INTEGER,       -- Number of succeeded tasks
    failed_count      INTEGER,       -- Number of failed tasks
    node_name         TEXT,          -- Job execution node name (comma-separated list). Watcher cumulatively records at RUNNING transition and when sweep succeeded_count/failed_count changes. Fetched at completion transition if RUNNING is skipped. Single node name for regular jobs; all used node names for sweep jobs
    cpu_millicores    INTEGER,       -- Parsed numeric value of cpu string (millicores). "500m" → 500, "2" → 2000. Used in Dispatcher in-flight CTE
    memory_mib        INTEGER,       -- Parsed numeric value of memory string (MiB). "4Gi" → 4096, "500Mi" → 500. Used in Dispatcher in-flight CTE
    PRIMARY KEY (namespace, job_id)
);

-- Index for fast lookup by k8s_job_name (used for orphan Job detection, API responses, etc.)
-- Note: Watcher uses the cjob.io/job-id label for job identification (not k8s_job_name matching)
CREATE INDEX idx_jobs_k8s_job_name ON jobs (k8s_job_name);

-- Index to optimize Dispatcher dispatch budget calculation
CREATE INDEX idx_jobs_namespace_status ON jobs (namespace, status);
```

`completions IS NULL` distinguishes regular jobs from sweep jobs. For sweep jobs, `completed_indexes` / `failed_indexes` are written by the Watcher from K8s API `status.completedIndexes` / `status.failedIndexes` (compressed notation strings). `succeeded_count` / `failed_count` are cache columns for referencing aggregate values without parsing `completed_indexes`.

`cpu_millicores` / `memory_mib` are denormalized numeric representations of the `cpu` / `memory` string columns, set by the Submit API at job creation using `parse_cpu_millicores()` / `parse_memory_mib()`. Used in the Dispatcher DRF query to aggregate predicted consumption of DISPATCHING/DISPATCHED jobs within SQL (see [dispatcher.md](dispatcher.md) §1.2).

## 2. `user_job_counters` Table

Per-user job_id assignment counter. Resets to 1 on reset.

```sql
CREATE TABLE user_job_counters (
    namespace   TEXT PRIMARY KEY,
    next_id     INTEGER NOT NULL DEFAULT 1
);
```

Assignment is performed atomically by the Submit API.

```sql
-- Assignment query (uses RETURNING to atomically assign and increment to prevent conflicts)
INSERT INTO user_job_counters (namespace, next_id)
VALUES (:namespace, 2)
ON CONFLICT (namespace) DO UPDATE
    SET next_id = user_job_counters.next_id + 1
RETURNING next_id - 1;   -- The issued job_id
```

## 3. `job_events` Table

```sql
CREATE TABLE job_events (
    id           BIGSERIAL PRIMARY KEY,
    namespace    TEXT NOT NULL,
    job_id       INTEGER NOT NULL,
    event_type   TEXT NOT NULL,
    payload_json JSONB NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (namespace, job_id) REFERENCES jobs(namespace, job_id)
        ON DELETE CASCADE   -- job_events are also deleted when the jobs record is deleted
);
```

## 4. `namespace_weights` Table

Manages fair sharing weights per namespace. Namespaces with larger weights have smaller DRF dominant shares for the same cumulative consumption, resulting in higher dispatch priority.

```sql
CREATE TABLE namespace_weights (
    namespace TEXT PRIMARY KEY,
    weight    REAL NOT NULL DEFAULT 1.0
);
```

In the Dispatcher DRF sort, sorting is done by `dominant_share / weight`. Namespaces without rows in the table are treated as weight = 1 (`COALESCE(w.weight, 1)`).

- **weight = 0**: Excluded from dispatch targets (usage prohibited). Jobs remain QUEUED; dispatch resumes when weight is set back to a value greater than 0. Administrators can use this to prevent a specific user from monopolizing the entire cluster by setting other users' weights to 0.
- **weight > 0**: Namespaces with larger weights have smaller dominant shares for the same cumulative consumption, resulting in higher dispatch priority. For example, a namespace with weight = 2 is dispatched preferentially over a namespace with weight = 1 until it consumes more resources. Decimal values (e.g., 1.5) can be specified.

## 5. `namespace_daily_usage` Table

Records per-namespace daily resource consumption. Used for Dispatcher fair sharing (dispatch priority adjustment). Calculates the total for the most recent `FAIR_SHARE_WINDOW_DAYS` days using a sliding window to determine the DRF dominant share.

Independent from the `jobs` table and unaffected by jobs record deletion via `cjob reset`.

```sql
CREATE TABLE namespace_daily_usage (
    namespace              TEXT NOT NULL,
    usage_date             DATE NOT NULL,
    flavor                 TEXT NOT NULL DEFAULT 'cpu',
    cpu_millicores_seconds BIGINT NOT NULL DEFAULT 0,
    memory_mib_seconds     BIGINT NOT NULL DEFAULT 0,
    gpu_seconds            BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (namespace, usage_date, flavor)
);
```

### 5.1 Column Descriptions

| Column | Type | Description |
|---|---|---|
| `namespace` | TEXT | User's namespace |
| `usage_date` | DATE | Date consumption was recorded (UTC) |
| `flavor` | TEXT | ResourceFlavor name. Recorded to apply `flavor_quotas.drf_weight` during DRF calculation |
| `cpu_millicores_seconds` | BIGINT | Daily total of `time_limit_seconds × cpu (in millicores)`. "2" → 2000, "0.5" → 500 |
| `memory_mib_seconds` | BIGINT | Daily total of `time_limit_seconds × memory (in MiB)`. "4Gi" → 4096, "500Mi" → 500 |
| `gpu_seconds` | BIGINT | Daily total of `time_limit_seconds × gpu (count)` |

### 5.2 Addition Processing

When the Watcher transitions a job to RUNNING, it adds the current day's consumption in the same transaction as recording `started_at`.

Addition calculation: `time_limit_seconds × resource amount` (Method C: reservation only, no return). For sweep jobs: `time_limit_seconds × resource amount × parallelism` (reflecting the maximum amount of simultaneously used resources). No return even if the job completes earlier than `time_limit_seconds`. This creates an incentive for users to accurately estimate `time_limit_seconds` and improves gap filling estimation accuracy.

No special processing is needed for CANCELLED. Cancellations before RUNNING have not been added, and cancellations during RUNNING have already been added and are not returned.

```sql
INSERT INTO namespace_daily_usage (namespace, usage_date, flavor, cpu_millicores_seconds, memory_mib_seconds, gpu_seconds)
VALUES (:namespace, CURRENT_DATE, :flavor, :delta_cpu, :delta_mem, :delta_gpu)
ON CONFLICT (namespace, usage_date, flavor) DO UPDATE SET
    cpu_millicores_seconds = namespace_daily_usage.cpu_millicores_seconds + EXCLUDED.cpu_millicores_seconds,
    memory_mib_seconds     = namespace_daily_usage.memory_mib_seconds + EXCLUDED.memory_mib_seconds,
    gpu_seconds            = namespace_daily_usage.gpu_seconds + EXCLUDED.gpu_seconds;
```

Atomic UPSERT: INSERT on the first occurrence of the day, then addition thereafter.

### 5.3 Window Aggregation

When the Dispatcher calculates the DRF dominant share in `fetch_dispatchable_jobs()`, it aggregates consumption for the most recent `FAIR_SHARE_WINDOW_DAYS` days.

```sql
-- For window_days=7: rows after CURRENT_DATE - 7 = 7 days covering 6 days ago through today
-- (Rows exactly 7 days ago are deleted in §5.4 and also excluded by this condition)
-- Aggregated per (namespace, flavor) unit (drf_weight is applied by Dispatcher after GREATEST calculation)
SELECT u.namespace, u.flavor,
       SUM(u.cpu_millicores_seconds) AS cpu_millicores_seconds,
       SUM(u.memory_mib_seconds) AS memory_mib_seconds,
       SUM(u.gpu_seconds) AS gpu_seconds
FROM namespace_daily_usage u
WHERE u.usage_date > CURRENT_DATE - :window_days
GROUP BY u.namespace, u.flavor
```

Old days outside the window naturally fall out of the aggregation. Each day, the oldest day drops out, preventing a sudden cliff reset.

### 5.4 Deletion of Old Rows

Immediately before the Dispatcher executes `fetch_dispatchable_jobs()`, it deletes old rows outside the retention period. The retention period is controlled by `USAGE_RETENTION_DAYS` (default 7). It is independent from the DRF calculation window (`FAIR_SHARE_WINDOW_DAYS`), allowing longer retention of consumption data for future use cases beyond DRF (e.g., usage statistics).

```sql
DELETE FROM namespace_daily_usage
WHERE usage_date <= CURRENT_DATE - :retention_days;
```

### 5.5 Design Decisions

- **Independence from jobs table**: No FK, so unaffected by `cjob reset`'s `DELETE FROM jobs`
- **Daily partitioning**: The sliding window eliminates the sudden cliff of a bulk reset (the problem where everyone's consumption becomes 0 immediately after a reset). Each day, the oldest day naturally drops out, smoothing consumption changes
- **Separate columns per resource type**: CPU, memory, and GPU have different consumption patterns, so they are separated to allow the Dispatcher to set weights flexibly
- **BIGINT sufficiency**: Even `time_limit_seconds` (max 604800) × `cpu_millicores` (max 300000) gives at most approximately 1.8 × 10^11 per day. BIGINT (max 9.2 × 10^18) is sufficient
- **flavor column**: DRF calculation independently computes dominant share per flavor, so consumption is recorded separately per flavor. `drf_weight` is applied at calculation time rather than at recording time, so weight changes immediately reflect in existing historical data
- **Separate retention period**: Making `USAGE_RETENTION_DAYS` independent from `FAIR_SHARE_WINDOW_DAYS` allows longer retention of consumption data for use cases beyond DRF (usage statistics, etc.)
- **Row count estimate**: namespace count × window days × flavor count. Approximately 20 namespaces × 7 days × 3 flavors = ~420 rows, making aggregation query cost negligible

## 6. `node_resources` Table

Records effective allocatable resources per compute node in the cluster (the value obtained by subtracting DaemonSet Pod requests from `allocatable`). The Watcher periodically fetches node information from the K8s API (`NODE_RESOURCE_SYNC_INTERVAL_SEC`, default 300 seconds) and updates via UPSERT.

```sql
CREATE TABLE node_resources (
    node_name           TEXT PRIMARY KEY,
    cpu_millicores      INTEGER NOT NULL,    -- Effective allocatable CPU (millicores, after subtracting DaemonSet Pod requests)
    memory_mib          INTEGER NOT NULL,    -- Effective allocatable memory (MiB, after subtracting DaemonSet Pod requests)
    gpu                 INTEGER NOT NULL DEFAULT 0,  -- Allocatable GPU (nvidia.com/gpu)
    flavor              TEXT NOT NULL DEFAULT 'cpu', -- ResourceFlavor name (matches name in RESOURCE_FLAVORS config)
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

CPU and memory store the effective allocatable after subtracting DaemonSet Pod (calico-node, kube-proxy, etc.) requests. This ensures that the Submit API reject validation, Dispatcher DRF normalization, and cjobctl `set-quota` validation all operate on "the amount actually available for jobs." GPU subtraction is not performed since DaemonSet-originated consumption is not expected.

The `flavor` column is set by the Watcher based on the source selector used to fetch nodes. For nodes fetched by the `label_selector` of each flavor definition in the `RESOURCE_FLAVORS` config (see [resources.md](resources.md)), that flavor's `name` is set. `DEFAULT 'cpu'` ensures backward compatibility with existing data.

### 6.1 Sync Processing

The Watcher fetches `status.allocatable` for nodes matching the `label_selector` of each flavor definition in the `RESOURCE_FLAVORS` config (see [resources.md](resources.md)), calculates effective allocatable by subtracting DaemonSet Pod CPU/memory requests obtained via `list_pod_for_all_namespaces()`, and UPSERTs per node. Nodes that exist in the DB but have disappeared from K8s (decommissioned, label removed) are DELETEd. See [watcher.md](watcher.md) §1.1 for details.

```sql
-- UPSERT (per node)
INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu, flavor, updated_at)
VALUES (:name, :cpu, :mem, :gpu, :flavor, NOW())
ON CONFLICT (node_name) DO UPDATE SET
    cpu_millicores = :cpu,
    memory_mib = :mem,
    gpu = :gpu,
    flavor = :flavor,
    updated_at = NOW();

-- Delete nodes that have disappeared from K8s
DELETE FROM node_resources WHERE node_name != ALL(:current_node_names);
```

### 6.2 Access Patterns

**Submit API (resource over-limit reject validation)**: Fetches the maximum value of each resource restricted to nodes of the specified flavor. Combined with the nominalQuota from the `flavor_quotas` table, determines the effective upper limit as `min(max node allocatable, nominalQuota)`. Rejects with 400 if requested resources exceed the effective upper limit.

```sql
SELECT MAX(cpu_millicores) AS max_cpu,
       MAX(memory_mib) AS max_memory,
       MAX(gpu) AS max_gpu
FROM node_resources
WHERE flavor = :flavor;
```

**Dispatcher (DRF normalization)**: Fetches the total allocatable per flavor, compares with the nominalQuota from the `flavor_quotas` table (§7), and sums `MIN(allocatable, nominalQuota)` across all flavors. This normalizes by the actually usable resource amount when nominalQuota is smaller than allocatable. If `flavor_quotas` is empty, uses the allocatable total as-is; flavors not in `flavor_quotas` are added with their allocatable as-is without quota constraints.

```sql
-- Total allocatable per flavor
SELECT flavor,
       COALESCE(SUM(cpu_millicores), 0) AS total_cpu,
       COALESCE(SUM(memory_mib), 0) AS total_memory,
       COALESCE(SUM(gpu), 0) AS total_gpu
FROM node_resources
GROUP BY flavor;

-- nominalQuota (same query as §7.2)
SELECT flavor, cpu, memory, gpu FROM flavor_quotas;
```

On the Python side, `MIN(allocatable, nominalQuota)` is calculated per flavor and maintained as per-flavor capacity along with `drf_weight` (via the `_fetch_flavor_caps()` function). TEXT values for nominalQuota are parsed using `parse_cpu_millicores()` / `parse_memory_mib()`.

**cjobctl (total allocatable per flavor)**: In `set-quota` validation, fetches the total allocatable for the node group corresponding to the specified flavor. Since the flavor name is unified with the Kueue ResourceFlavor name, it can be used directly in queries without conversion.

CPU is summed after rounding down to integer core units per node, reflecting bin-packing constraints. Fractional cores per node (e.g., the 0.633 core surplus after subtracting DaemonSet Pod requests) cannot be consumed by integer-core jobs, and allowing nominalQuota up to the total including fractions would result in "jobs that have capacity in the cluster-wide quota but cannot be placed on any node and remain waiting as DISPATCHED." Memory and GPU are summed directly (memory is already granular enough in MiB units, GPU is already integer).

```sql
SELECT COALESCE(SUM((cpu_millicores / 1000) * 1000), 0) AS total_cpu,
       COALESCE(SUM(memory_mib), 0) AS total_memory,
       COALESCE(SUM(gpu), 0) AS total_gpu
FROM node_resources
WHERE flavor = :flavor;
```

### 6.3 Design Decisions

- **Per-node rows**: Submit API reject validation requires "maximum allocatable of a single node"; cluster totals alone are insufficient. Maintaining per-node data enables both MAX()-based reject validation and SUM()-based DRF normalization from a single table
- **updated_at**: Used in cjobctl to check the freshness of node information. Enables detection of stale data when the Watcher stops
- **Row count estimate**: Same as the number of compute nodes. Approximately 10-50 nodes are assumed, making query cost negligible
- **Fallback when table is empty**: When Watcher is not running, `node_resources` is empty. The Submit API skips validation, and the Dispatcher disables DRF sorting and falls back to namespace name order. This allows the system to operate before the Watcher starts
- **Unified flavor naming**: Values of `node_resources.flavor` and `jobs.flavor` must match Kueue ResourceFlavor `metadata.name`. This eliminates the need for name conversion between DB queries and the Kueue API
- **Reason for subtracting DaemonSet Pod requests**: `status.allocatable` includes DaemonSet Pod (calico-node, kube-proxy, etc.) requests and thus exceeds "the amount actually available for jobs." Subtracting them in the Watcher and recording the effective allocatable ensures that Submit API resource upper limit validation (max allocatable of a single node) and cjobctl `set-quota` validation (total allocatable per flavor) reflect reality. This prevents the situation where "the limit display shows it should be submittable, but the Kubernetes scheduler cannot place it on any node"

## 7. `flavor_quotas` Table

Records the nominalQuota for each ResourceFlavor in the ClusterQueue. The Watcher periodically fetches the ClusterQueue from the K8s API (same cycle as `node_resources`) and updates via UPSERT.

```sql
CREATE TABLE IF NOT EXISTS flavor_quotas (
    flavor      TEXT PRIMARY KEY,
    cpu         TEXT NOT NULL,
    memory      TEXT NOT NULL,
    gpu         TEXT NOT NULL DEFAULT '0',
    drf_weight  REAL NOT NULL DEFAULT 1.0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

`cpu`, `memory`, and `gpu` store nominalQuota values as K8s resource quantity strings (e.g., `"256"`, `"1000Gi"`, `"4"`). They are stored as-is for direct display in the CLI, avoiding information loss from parse→restore (e.g., 1000Gi → 1024000 MiB → unrestorable).

`drf_weight` is a weight coefficient applied to this flavor's resource consumption and capacity during DRF calculation. Default 1.0. Set larger values for precious resources like GPU (e.g., 2.0), and smaller values for low-spec flavors (e.g., 0.5). The Watcher sync only updates `cpu`, `memory`, `gpu`, and `updated_at`, so `drf_weight` is not affected. Set via `cjobctl cluster set-drf-weight`.

### 7.1 Sync Processing

The Watcher fetches the ClusterQueue via `CustomObjectsApi.get_cluster_custom_object()`, reads the nominalQuota from `resources[]` for each flavor in `spec.resourceGroups[0].flavors[]`, and UPSERTs. Flavors that exist in the DB but not in the ClusterQueue are DELETEd.

```sql
-- UPSERT (per flavor)
INSERT INTO flavor_quotas (flavor, cpu, memory, gpu, updated_at)
VALUES (:flavor, :cpu, :memory, :gpu, NOW())
ON CONFLICT (flavor) DO UPDATE SET
    cpu = :cpu,
    memory = :memory,
    gpu = :gpu,
    updated_at = NOW();

-- Delete flavors that have disappeared from ClusterQueue
DELETE FROM flavor_quotas WHERE flavor != ALL(:current_flavors);
```

### 7.2 Access Patterns

**Submit API (resource over-limit reject validation)**: Fetches the nominalQuota for the specified flavor at job submission and, combined with the MAX value from `node_resources`, determines the effective upper limit as `min(max_node_allocatable, nominalQuota)`. For sweeps, determines the cluster-wide check upper limit as `min(total allocatable, nominalQuota)`.

```sql
SELECT cpu, memory, gpu
FROM flavor_quotas
WHERE flavor = :flavor;
```

**Submit API (`GET /v1/flavors`)**: Fetches the nominalQuota for each flavor and returns it to the CLI. The CLI combines it with the MAX value from `node_resources` to calculate and display the per-task resource upper limit (`min(max_node_allocatable, nominalQuota)`).

```sql
SELECT flavor, cpu, memory, gpu
FROM flavor_quotas;
```

**Dispatcher (DRF flavor weight)**: Fetches per-flavor capacity (`MIN(allocatable, nominalQuota)`) and `drf_weight` for use in per-flavor dominant share calculation. `drf_weight` is not directly applied to consumption or capacity, but applied to the per-flavor dominant share (`GREATEST(cpu_share, mem_share, gpu_share)`) before summing across all flavors.

```sql
SELECT flavor, cpu, memory, gpu, drf_weight FROM flavor_quotas;
```

**cjobctl (DRF weight setting)**: Updates `drf_weight` for the specified flavor.

```sql
UPDATE flavor_quotas SET drf_weight = :weight WHERE flavor = :flavor;
```

### 7.3 Design Decisions

- **TEXT storage**: nominalQuota is stored as K8s resource quantity strings as-is. "1000Gi" can be displayed directly in the CLI, avoiding information loss from numeric parse→restore (e.g., 1000Gi → 1024000 MiB → unrestorable). No resource quantity arithmetic in the DB is needed
- **Fallback when table is empty**: When the Watcher has not synced, `flavor_quotas` is empty. Submit API resource validation uses only `node_resources` allocatable for judgment. `GET /v1/flavors` returns `quota: null`, and the CLI displays "Resource information has not been retrieved yet"
- **drf_weight separation**: `drf_weight` is a value set by administrators, not fetched from Kueue, so it is excluded from Watcher sync and set individually via `cjobctl cluster set-drf-weight`. If the Watcher DELETEs a flavor, its `drf_weight` is also deleted; if INSERTed, the default 1.0 is applied
- **Row count estimate**: Same as the number of flavors. Approximately 2-5 flavors are assumed, making query cost negligible

## 8. `namespace_resource_quotas` Table

Records ResourceQuota usage status for each user namespace. The Watcher periodically fetches ResourceQuota from the K8s API (same cycle as `node_resources`) and updates via UPSERT. Used by the Dispatcher to check remaining resources before dispatch and hold jobs as QUEUED when insufficient.

```sql
CREATE TABLE namespace_resource_quotas (
    namespace            TEXT PRIMARY KEY,
    hard_cpu_millicores  INTEGER NOT NULL,
    hard_memory_mib      INTEGER NOT NULL,
    hard_gpu             INTEGER NOT NULL DEFAULT 0,
    hard_count           INTEGER,              -- hard value of count/jobs.batch. NULL = no limit
    used_cpu_millicores  INTEGER NOT NULL,
    used_memory_mib      INTEGER NOT NULL,
    used_gpu             INTEGER NOT NULL DEFAULT 0,
    used_count           INTEGER,              -- used value of count/jobs.batch. NULL = no limit
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

`hard_*` stores parsed numeric values from `spec.hard`, and `used_*` stores parsed numeric values from `status.used`. CPU in millicores, memory in MiB, GPU in units. The reason for storing as numeric values (same as `node_resources`) is that the Dispatcher calculates remaining resources (hard - used) on the Python side and compares with job resource requests.

`hard_count` / `used_count` store `count/jobs.batch` ResourceQuota values. If `count/jobs.batch` is not set in ResourceQuota, both columns are `NULL`, and the Dispatcher treats the namespace as having no Job count limit. Unlike CPU/memory/GPU which store `0` when unset, `count/jobs.batch` is optional in ResourceQuota, so `NULL` explicitly indicates "not set."

### 8.1 Sync Processing

The Watcher fetches all user namespaces with the `USER_NAMESPACE_LABEL` label from the K8s API, reads each namespace's ResourceQuota from the K8s API, and UPSERTs. All user namespaces are tracked regardless of whether they have jobs (to track resource consumption by JupyterHub User Pods, etc. before job submission). Rows for namespaces that are no longer user namespaces are DELETEd.

```sql
-- UPSERT (per namespace)
INSERT INTO namespace_resource_quotas
(namespace, hard_cpu_millicores, hard_memory_mib, hard_gpu, hard_count,
 used_cpu_millicores, used_memory_mib, used_gpu, used_count, updated_at)
VALUES (:ns, :h_cpu, :h_mem, :h_gpu, :h_count, :u_cpu, :u_mem, :u_gpu, :u_count, NOW())
ON CONFLICT (namespace) DO UPDATE SET
    hard_cpu_millicores = :h_cpu, hard_memory_mib = :h_mem, hard_gpu = :h_gpu,
    hard_count = :h_count,
    used_cpu_millicores = :u_cpu, used_memory_mib = :u_mem, used_gpu = :u_gpu,
    used_count = :u_count,
    updated_at = NOW();

-- Delete namespaces that are no longer active
DELETE FROM namespace_resource_quotas WHERE namespace NOT IN (:active_namespaces);
```

Namespaces for which no ResourceQuota exists (K8s API returns 404) have their rows DELETEd. This causes the Dispatcher to treat that namespace as having no limits. For transient K8s API errors (500, etc.), existing data is retained and retried in the next cycle.

GPU values are fetched from ResourceQuota as `requests.{gpu_resource_name}` using the `gpu_resource_name` from each flavor definition in the `RESOURCE_FLAVORS` config (see [resources.md](resources.md)). When multiple GPU resource names are configured, the first non-zero value found is used.

`count/jobs.batch` is fetched directly from `spec.hard`. If `count/jobs.batch` is included in `spec.hard`, an integer value is stored in `hard_count` and an integer value from `status.used` in `used_count`. If not included in `spec.hard`, both columns store `NULL`.

### 8.2 Access Patterns

**Dispatcher (ResourceQuota pre-check)**: Fetches remaining resources for dispatch candidate namespaces and compares with job resource requests.

```sql
SELECT namespace, hard_cpu_millicores, hard_memory_mib, hard_gpu,
       used_cpu_millicores, used_memory_mib, used_gpu,
       hard_count, used_count
FROM namespace_resource_quotas
WHERE namespace IN (:candidate_namespaces);
```

Namespaces without rows in the table either have no ResourceQuota or the Watcher has not synced, and are dispatched as having no limits.

**Usage API (ResourceQuota display)**: Returns the self namespace's ResourceQuota usage status via `GET /v1/usage`.

```sql
SELECT hard_cpu_millicores, hard_memory_mib, hard_gpu, hard_count,
       used_cpu_millicores, used_memory_mib, used_gpu, used_count
FROM namespace_resource_quotas
WHERE namespace = :namespace;
```

If no row exists, `resource_quota` in the response is set to `null`.

**cjobctl (admin ResourceQuota list)**: Displays ResourceQuota usage status for all user namespaces via `cjobctl usage quota`. Fetches the user namespace list from the K8s API and cross-references with the DB.

```sql
SELECT namespace, hard_cpu_millicores, hard_memory_mib, hard_gpu,
       used_cpu_millicores, used_memory_mib, used_gpu, updated_at,
       hard_count, used_count
FROM namespace_resource_quotas
ORDER BY namespace;
```

Namespaces without rows in the DB are displayed as `-` indicating no ResourceQuota set.

### 8.3 Design Decisions

- **Pre-parsed numeric storage**: Same reason as `node_resources`. The Dispatcher calculates remaining resources (hard - used) on the Python side and compares with the job's `cpu_millicores` / `memory_mib` / `gpu`
- **Storing both hard/used**: Storing the original values rather than just remaining (hard - used) allows checking usage status in cjobctl display and debugging
- **No row = no limit**: When ResourceQuota does not exist for a namespace or the Watcher has not synced, the table is empty. The Dispatcher dispatches these namespaces as having no limits. Consistent with the fallback pattern of `node_resources` / `flavor_quotas`
- **Row count estimate**: Same as the number of active namespaces. Approximately 20 namespaces are assumed, making query cost negligible

## 9. State Transitions

```text
QUEUED
  ├─ HELD (user holds → Dispatcher skips. Returns to QUEUED on release)
  │    ├─ QUEUED (user releases hold)
  │    └─ CANCELLED (user cancels)
  ├─ CANCELLED (user cancels → Dispatcher skips on next scan)
  └─ DISPATCHING (when Dispatcher selects via DB scan and updates to DISPATCHING)
       ├─ CANCELLED (user cancels → skipped before CAS, Watcher deletes K8s Job after CAS)
       ├─ DISPATCHED (Kubernetes Job creation succeeded)
       │    ├─ CANCELLED (user cancels → Watcher deletes K8s Job)
       │    └─ RUNNING (Watcher detects Pod running)
       │         ├─ SUCCEEDED
       │         ├─ FAILED
       │         └─ CANCELLED (user cancels → Watcher deletes K8s Job)
       ├─ QUEUED (on retry: Dispatcher restart / retry_after rollback after K8s transient failure)
       └─ FAILED (validation error / max retry exceeded)
CANCELLED (user cancels at any point in QUEUED / DISPATCHING / DISPATCHED / RUNNING)
CANCELLED / SUCCEEDED / FAILED
  └─ DELETING (after POST /v1/reset, transitions in bulk from these 3 states · waiting for K8s Job deletion and DB cleanup by Watcher)
       └─ (after deletion completes, Watcher deletes DB records and resets counter)
```
