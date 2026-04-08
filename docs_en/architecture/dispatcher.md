> *This document was auto-translated from the [Japanese original](../../docs/architecture/dispatcher.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# Dispatcher Design

## 1. Scheduling Design

### 1.1 Scheduling Policy

The Dispatcher periodically scans PostgreSQL and selects jobs to dispatch based on the following criteria.

1. **Only target (namespace, flavor) pairs with available budget** (DISPATCHING + DISPATCHED + RUNNING for the same (namespace, flavor) < dispatch_limit)
2. **Retrieve QUEUED jobs for the target namespace in ascending `created_at` order**
3. **Fix the number of jobs fetched per cycle at `DISPATCH_BATCH_SIZE` (default 50)**
4. **Round-robin fairly across namespaces** (fetch `DISPATCH_ROUND_SIZE` jobs alternately from each namespace)
5. **Prioritize namespaces with a smaller cumulative resource consumption dominant share** (fairness via DRF)

This approach ensures:
- Jobs from users who have exhausted their budget do not block other users
- Submission order within the same user (`created_at` ascending) is always preserved
- Multiple users with jobs in QUEUED state are handled fairly
- The `DISPATCH_ROUND_SIZE` setting allows tuning from uniform round-robin distribution to consumption-based priority control via DRF (see §1.2 Tuning Guidelines)
- The number of dispatches per cycle is fixed, making K8s API load predictable

**Fair sharing (DRF):** The Dispatcher refers to each namespace's resource consumption over the past `FAIR_SHARE_WINDOW_DAYS` days (the `namespace_daily_usage` table in [database.md](database.md) §5) and determines dispatch priority based on Dominant Resource Fairness (DRF). Each resource (CPU, memory, GPU) is normalized by the total cluster capacity, and namespaces with a smaller maximum value (dominant share) divided by the namespace weight ([database.md](database.md) §4) are dispatched first. This causes namespaces that have consumed more resources to receive lower priority, allowing namespaces with less consumption to receive resources. Namespaces with larger weights remain prioritized until they consume proportionally more resources. Because daily consumption is aggregated using a sliding window, there are no sharp resets.

**Flavor DRF weight:** The DRF score computes the dominant share per flavor and multiplies it by the `drf_weight` from the `flavor_quotas` table ([database.md](database.md) §7), then sums across all flavors. This accurately reflects differences in the "value" of resources per flavor. By assigning a large weight (e.g., 2.0) to scarce resources like GPUs and a small weight (e.g., 0.5) to low-spec flavors, resource scarcity in high-value flavors is appropriately reflected in the score. The default is 1.0 (uniform across all flavors). Set with `cjobctl cluster set-drf-weight`.

```
dominant_share = Σ_f ( GREATEST(consumed_cpu_f / capacity_cpu_f,
                                consumed_mem_f / capacity_mem_f,
                                consumed_gpu_f / capacity_gpu_f) × drf_weight_f )
```

This approach ensures that resource consumption in smaller flavors is not diluted by the capacity of larger flavors, and per-flavor resource scarcity is accurately reflected in the DRF score.

The per-flavor capacity used for DRF normalization is the smaller of the allocatable total from the `node_resources` table ([database.md](database.md) §6) and the nominalQuota from the `flavor_quotas` table ([database.md](database.md) §7) (weight is not applied here; weight is applied after the dominant share calculation). Because the Watcher periodically synchronizes node `allocatable` and ClusterQueue nominalQuota, node additions/removals and quota changes are automatically reflected. If the `flavor_quotas` table is empty (Watcher not yet synchronized), the allocatable total from `node_resources` is used as-is (treated as weight 1.0). If the `node_resources` table is also empty, DRF sorting is disabled and falls back to ordering by namespace name.

**Retention of consumption data:** Deletion of old rows from `namespace_daily_usage` is controlled by `USAGE_RETENTION_DAYS` (default 7). This is independent of the DRF calculation window (`FAIR_SHARE_WINDOW_DAYS`), allowing longer retention of consumption data for future use cases beyond DRF.

### 1.2 DB Scan Query Policy

```sql
-- flavor_caps CTE: per-flavor capacity and weight computed on the Python side, injected via VALUES clause
WITH flavor_caps(flavor, cap_cpu, cap_mem, cap_gpu, w) AS (
  VALUES (:f_0::TEXT, :cpu_0::FLOAT, :mem_0::FLOAT, :gpu_0::FLOAT, :w_0::FLOAT),
         (:f_1::TEXT, :cpu_1::FLOAT, :mem_1::FLOAT, :gpu_1::FLOAT, :w_1::FLOAT)
         -- ... one row per flavor
),
-- active CTE: aggregate active job count per (namespace, flavor) for budget control
active AS (
  SELECT namespace, flavor, COUNT(*) AS active_count
  FROM jobs
  WHERE status IN ('DISPATCHING', 'DISPATCHED', 'RUNNING')
  GROUP BY namespace, flavor
),
-- queued CTE: assign per-namespace submission order (rn) and per-(namespace, flavor) order (flavor_rn) to QUEUED jobs
queued AS (
  SELECT *,
    ROW_NUMBER() OVER (PARTITION BY namespace ORDER BY created_at ASC) AS rn,
    ROW_NUMBER() OVER (PARTITION BY namespace, flavor ORDER BY created_at ASC) AS flavor_rn
  FROM jobs
  WHERE status = 'QUEUED'            -- HELD jobs are excluded from dispatch
    AND (retry_after IS NULL OR retry_after <= NOW())
),
-- usage CTE: windowed aggregation for the past N days (separated by flavor, no drf_weight multiplication)
usage AS (
  SELECT u.namespace, u.flavor,
    SUM(u.cpu_millicores_seconds) AS cpu_ms,
    SUM(u.memory_mib_seconds) AS mem_ms,
    SUM(u.gpu_seconds) AS gpu_s
  FROM namespace_daily_usage u
  WHERE u.usage_date > CURRENT_DATE - :window_days
  GROUP BY u.namespace, u.flavor
),
-- in_flight CTE: aggregate estimated consumption of DISPATCHING/DISPATCHED jobs (separated by flavor, no drf_weight multiplication)
in_flight AS (
  SELECT j.namespace, j.flavor,
    SUM(j.time_limit_seconds * j.cpu_millicores
        * CASE WHEN j.completions IS NOT NULL THEN j.parallelism ELSE 1 END
    ) AS cpu_ms,
    SUM(j.time_limit_seconds * j.memory_mib
        * CASE WHEN j.completions IS NOT NULL THEN j.parallelism ELSE 1 END
    ) AS mem_ms,
    SUM(j.time_limit_seconds * j.gpu
        * CASE WHEN j.completions IS NOT NULL THEN j.parallelism ELSE 1 END
    ) AS gpu_s
  FROM jobs j
  WHERE j.status IN ('DISPATCHING', 'DISPATCHED')
  GROUP BY j.namespace, j.flavor
),
-- drf_scores CTE: compute dominant share per flavor, multiply by weight, sum per namespace
drf_scores AS (
  SELECT nfc.namespace,
    SUM(
      GREATEST(
        nfc.total_cpu / fc.cap_cpu,
        nfc.total_mem / fc.cap_mem,
        nfc.total_gpu / NULLIF(fc.cap_gpu, 0)
      ) * fc.w
    ) AS drf_score
  FROM (
    SELECT COALESCE(u.namespace, inf.namespace) AS namespace,
           COALESCE(u.flavor, inf.flavor) AS flavor,
           COALESCE(u.cpu_ms, 0) + COALESCE(inf.cpu_ms, 0) AS total_cpu,
           COALESCE(u.mem_ms, 0) + COALESCE(inf.mem_ms, 0) AS total_mem,
           COALESCE(u.gpu_s, 0) + COALESCE(inf.gpu_s, 0) AS total_gpu
    FROM usage u
    FULL OUTER JOIN in_flight inf ON u.namespace = inf.namespace AND u.flavor = inf.flavor
  ) nfc
  JOIN flavor_caps fc ON nfc.flavor = fc.flavor
  GROUP BY nfc.namespace
)
SELECT q.* FROM queued q
  LEFT JOIN active a ON q.namespace = a.namespace AND q.flavor = a.flavor
  LEFT JOIN drf_scores d ON q.namespace = d.namespace
  LEFT JOIN namespace_weights w ON q.namespace = w.namespace
WHERE COALESCE(a.active_count, 0) < :dispatch_limit              -- only (namespace, flavor) with available budget
  AND q.flavor_rn <= :dispatch_limit - COALESCE(a.active_count, 0)  -- fetch only up to remaining budget (per flavor)
  AND COALESCE(w.weight, 1) > 0                                   -- exclude namespaces with weight=0 from dispatch
ORDER BY CEIL(q.rn * 1.0 / :round_size) ASC,  -- round-robin (fetch round_size jobs alternately from each namespace)
         COALESCE(d.drf_score, 0)              -- DRF: weighted sum of per-flavor dominant shares
           / COALESCE(w.weight, 1)             -- divide by namespace weight
           ASC NULLS FIRST,                    -- namespaces with no consumption records (NULL) have highest priority
         q.namespace ASC                       -- deterministic tie-breaking by namespace name
LIMIT :fetch_limit;                            -- fetch excess candidates (DISPATCH_BATCH_SIZE × DISPATCH_FETCH_MULTIPLIER)
```

This query is optimized by the `idx_jobs_namespace_status` index. The `flavor_caps` CTE injects per-flavor capacity computed by `_fetch_flavor_caps()` on the Python side as bind parameters in the VALUES clause (dynamically constructed with one row per flavor). The `usage` CTE and `in_flight` CTE aggregate at the (namespace, flavor) level without multiplying by `drf_weight` (weight is applied in the `drf_scores` CTE after the GREATEST calculation). The `drf_scores` CTE integrates `usage` and `in_flight` via FULL OUTER JOIN, JOINs with `flavor_caps` to compute the dominant share per flavor, multiplies by `w`, and SUMs per namespace. The number of consumption data rows is approximately 20 namespaces × 7 days × 3 flavors = 420 rows, making the additional cost of FULL OUTER JOIN and GREATEST negligible.

**Per-flavor budget separation:** The `active` CTE aggregates active job counts per `(namespace, flavor)`, and the `queued` CTE assigns both a per-namespace `rn` and a per-`(namespace, flavor)` `flavor_rn`. The JOIN with `active` matches on `(namespace, flavor)`, and the budget condition in the WHERE clause uses `flavor_rn`. This ensures that even if active jobs of one flavor exhaust the budget, jobs of other flavors in the same namespace are dispatched with independent budgets. The `rn` used for round-robin remains at the namespace level, so namespaces with many flavors do not disproportionately occupy round-robin slots.

**Round-robin mechanism:** `ROW_NUMBER()` is assigned only to QUEUED jobs, and grouping by `CEIL(rn / round_size)` achieves alternating fetches of `DISPATCH_ROUND_SIZE` jobs from each namespace. `rn` is a per-namespace sequential number; per-flavor separation is handled only by the budget condition (`flavor_rn`). With the default (`round_size = 1`), one job is fetched alternately from each namespace; with `round_size = 5`, five jobs are fetched in a batch. The number of dispatches per cycle is capped at `DISPATCH_BATCH_SIZE` after downstream filtering (see "Excess candidate fetching" below).

**Excess candidate fetching:** The SQL `LIMIT` fetches `DISPATCH_BATCH_SIZE × DISPATCH_FETCH_MULTIPLIER` candidates (default 50 × 10 = 500). The fetched candidates pass through the gap-filling filter in §2.4 and the ResourceQuota pre-check in §2.5, then are trimmed to the first `DISPATCH_BATCH_SIZE` on the Python side for dispatch. Excess fetching ensures that when jobs from a DRF-prioritized namespace are all filtered out (cannot run with current remaining resources), candidates from other namespaces are still dispatched. This prevents the situation where resources are available yet 0 dispatches continue and fairness stagnates. The multiplier can be tuned based on the number of namespaces and the distribution of job sizes. The number of candidates is bounded above by `namespace count × flavor count × DISPATCH_BUDGET_PER_NAMESPACE` due to the WHERE clause condition `q.flavor_rn <= :dispatch_limit - active_count`, so it is not unbounded; only the network transfer volume from DB to Dispatcher and the Python-side filter iteration count increase.

**Fairness guarantee (DRF):** Dominant share is computed independently per flavor, multiplied by `drf_weight`, and then summed across all flavors. Specifically, consumption from the `usage` CTE and `in_flight` CTE is integrated via FULL OUTER JOIN at the (namespace, flavor) level, normalized by the capacity in the `flavor_caps` CTE, the dominant share is computed using `GREATEST`, multiplied by `w` (drf_weight), and `SUM`med per namespace. This aggregated value is divided by the namespace weight (from the `namespace_weights` table, default 1) for sorting. Per-flavor capacity is the smaller of the allocatable total from the `node_resources` table and the nominalQuota from the `flavor_quotas` table (see [database.md](database.md) §6.2 and §7.2). This approach ensures that resource scarcity in smaller flavors is not diluted by the capacity of larger flavors, and per-flavor resource usage is accurately reflected in the score. Namespaces without records in `namespace_daily_usage` are treated as having zero consumption (`COALESCE` + `NULLS FIRST`) and are dispatched first. For flavors where GPU is 0, `NULLIF(cap_gpu, 0)` makes the GPU term NULL, and since `GREATEST` ignores NULL arguments, it is excluded from the DRF calculation. If the `node_resources` table is empty, DRF sorting is disabled and falls back to ordering by namespace name.

**Reflecting estimated consumption via in-flight CTE:** DISPATCHING/DISPATCHED jobs are not yet recorded in `namespace_daily_usage` (the Watcher records them upon transition to RUNNING), but the in_flight CTE adds estimated consumption of `time_limit_seconds × resource amount` per (namespace, flavor) to the DRF score. Jobs that have transitioned to RUNNING are already recorded in `namespace_daily_usage` and are excluded from the `status IN ('DISPATCHING', 'DISPATCHED')` condition, preventing double-counting. PostgreSQL's MVCC (snapshot isolation) ensures consistency of status transitions within the same transaction. The in_flight CTE uses the `jobs.cpu_millicores` / `jobs.memory_mib` columns (numeric columns set by the Submit API via `parse_cpu_millicores()` / `parse_memory_mib()`) (see [database.md](database.md) §1). `drf_weight` is not multiplied inside the in_flight CTE; it is applied in the `drf_scores` CTE after the GREATEST calculation.

**Deleting old rows:** Before executing `fetch_dispatchable_jobs()`, old rows outside the retention period (`USAGE_RETENTION_DAYS`) are deleted (see [database.md](database.md) §5.4).

**`DISPATCH_ROUND_SIZE` tuning guidelines:** `DISPATCH_ROUND_SIZE` controls the balance between round-robin (primary sort) and DRF (secondary sort). The query's `ORDER BY` sorts by the following priority:

1. `CEIL(rn / round_size)` — round-robin group
2. DRF dominant share / weight — namespace priority within a group
3. Namespace name — deterministic tie-breaking

DRF materially affects dispatch results only when the number of jobs within a single round-robin group exceeds `DISPATCH_BATCH_SIZE` and truncation to `DISPATCH_BATCH_SIZE` occurs. A smaller `round_size` results in fewer jobs per group, limiting DRF to ordering adjustments. A larger `round_size` results in more jobs per group, allowing DRF to determine the allocation of dispatch slots itself.

| Setting | Behavior | Characteristics |
|---|---|---|
| `round_size = 1` (default) | Fetches 1 job alternately from each namespace. DRF determines only the order within a group | When the number of namespaces is ≤ `DISPATCH_BATCH_SIZE`, all namespaces are dispatched equally. DRF's effect appears only when the number of namespaces exceeds `DISPATCH_BATCH_SIZE` |
| `round_size = DISPATCH_BUDGET_PER_NAMESPACE` | All jobs within each namespace's budget fall into the same group; DRF fully determines allocation between namespaces | Namespaces with lower resource consumption are dispatched first; namespaces with higher consumption are throttled. As dispatch progresses, the in_flight CTE is updated and concentration on specific namespaces returns to equilibrium within a few cycles (tens of seconds). Even if jobs from the DRF-prioritized namespace are all filtered out, excess candidate fetching ensures other namespaces continue to be dispatched, preventing equilibrium stagnation due to 0 dispatches |

Intermediate values are not recommended because the influence of DRF varies depending on `DISPATCH_BATCH_SIZE mod (namespace count × round_size)` and becomes unstable as the number of namespaces changes. To intend consumption-based priority control via DRF, set `round_size = DISPATCH_BUDGET_PER_NAMESPACE`. To operate with round-robin only without DRF, keep the default `round_size = 1`.

### 1.3 Retry Management

Retries upon temporary K8s API failures are managed with the `jobs.retry_after` timestamp.
RabbitMQ DLQ/TTL is not required.

```sql
-- On temporary failure: set retry_after and revert to QUEUED
-- AND status = 'DISPATCHING' prevents overwriting CANCELLED
UPDATE jobs
SET retry_count = retry_count + 1,
    retry_after = NOW() + INTERVAL '30 seconds',  -- DISPATCH_RETRY_INTERVAL_SEC seconds later
    status = 'QUEUED'
WHERE namespace = :namespace
  AND job_id    = :job_id
  AND status    = 'DISPATCHING';   -- does not overwrite CANCELLED
```

The condition `retry_after IS NULL OR retry_after <= NOW()` ensures the job is automatically retried in the next scan.

## 2. Dispatcher Detailed Design

### 2.1 Role

The Dispatcher scans PostgreSQL to select QUEUED jobs and creates Kubernetes Jobs.

- Periodically scans the DB to select jobs for dispatch
- Performs fair scheduling across namespaces
- Checks dispatch budget and creates K8s Jobs
- Updates DB state on success or failure
- Resets DISPATCHING state on startup

The Dispatcher's main loop touches the `/tmp/liveness` file upon completion of each scan cycle. Kubernetes' Liveness probe checks the last modification time of this file to detect loop stalls and trigger a restart (see [deployment.md](../deployment.md) §13.4).

```text
dispatch_budget(namespace, flavor) = dispatch_limit - active_jobs_in_db(namespace, flavor)

dispatch_limit   = 32 (configured via ConfigMap: DISPATCH_BUDGET_PER_NAMESPACE, applied per flavor)
batch_size       = 50 (configured via ConfigMap: DISPATCH_BATCH_SIZE)
fetch_multiplier = 10 (configured via ConfigMap: DISPATCH_FETCH_MULTIPLIER)

active_jobs_in_db(namespace, flavor) is retrieved from PostgreSQL per (namespace, flavor).
K8s API is not queried.

Target statuses:
  - DISPATCHING (being processed by Dispatcher)
  - DISPATCHED (K8s Job created, waiting in Kueue)
  - RUNNING (Pod executing)
```

**Why DB-based approach is used:**

- If the Dispatcher queries the K8s API for every budget calculation, a K8s API failure would propagate to the entire Dispatcher
- Since the Dispatcher updates the status to DISPATCHING before creating the Job, submitted jobs are always reflected in the DB
- Although Watcher sync delays may cause the DB state to differ from reality by a few entries, a few seconds to 10-second discrepancy is negligible in practice compared to research computation runtimes (minutes to hours)
- The direction of discrepancy is always an underestimate of budget (dispatching conservatively), never an overestimate (over-dispatching)

DB queries are optimized by the `idx_jobs_namespace_status` index.

### 2.2 Retry Policy

Handling differs per failure scenario.

| Scenario | Action | Retry Interval | Limit |
|---|---|---|---|
| Temporary K8s API failure | Set `retry_after` and revert to `QUEUED` | After `DISPATCH_RETRY_INTERVAL_SEC` seconds | `DISPATCH_MAX_RETRIES` times |
| Insufficient dispatch budget | Re-evaluated in the next scan (naturally retried) | Every `DISPATCH_BUDGET_CHECK_INTERVAL_SEC` seconds | None (until budget recovers) |
| Validation error | Immediately FAILED | None | None |
| Permanent K8s error | Immediately FAILED | None | None |

#### Handling Temporary K8s API Failures

```python
# This is pseudocode for conceptual explanation.

except TemporaryK8sError:
    # Retrieve current retry_count and check against limit (before atomic UPDATE)
    # This ensures FAILED transition happens first, bypassing QUEUED
    current_count = db.get_retry_count(namespace, job_id)
    if current_count + 1 >= max_retries:
        # AND status='DISPATCHING' condition prevents overwriting CANCELLED
        updated_rows = db.update_status(
            namespace, job_id, "FAILED",
            error="max retries exceeded", condition_status="DISPATCHING"
        )
        # updated_rows == 0 means cancel API already updated to CANCELLED, skip
        return
    # Within limit: atomically update retry_count, retry_after, and status (see §1.3)
    # AND status='DISPATCHING' condition prevents overwriting CANCELLED
    updated_rows = db.increment_retry_and_set_queued(
        namespace, job_id,
        retry_after=now + int(os.environ["DISPATCH_RETRY_INTERVAL_SEC"])
    )
    if updated_rows == 0:
        return   # cancel API already updated to CANCELLED → skip
    db.record_event(namespace, job_id, "RETRY", {"count": current_count + 1})
```

Once `retry_after <= NOW()`, the job automatically becomes a re-dispatch candidate in the next scan.

### 2.3 Dispatch Loop

```python
# This is pseudocode for conceptual explanation.

class Dispatcher:
    def __init__(self):
        self.check_interval = int(os.environ["DISPATCH_BUDGET_CHECK_INTERVAL_SEC"])
        self.batch_size = int(os.environ["DISPATCH_BATCH_SIZE"])
        self.fetch_multiplier = int(os.environ["DISPATCH_FETCH_MULTIPLIER"])

    def run(self):
        while True:
            # Query from §1.2 (retention reset → round-robin/DRF priority/LIMIT fetch_limit for excess fetching)
            candidates = db.fetch_dispatchable_jobs()
            # Gap-filling filter from §2.4 (limit candidates for namespaces with stalled jobs)
            candidates = apply_gap_filling(session, candidates, settings)
            # ResourceQuota pre-check from §2.5 (limit candidates based on namespace remaining resources)
            candidates = filter_by_resource_quota(session, candidates)
            # Trim to first DISPATCH_BATCH_SIZE after filtering (cap on dispatches per cycle)
            candidates = candidates[:self.batch_size]
            for job in candidates:
                self.dispatch(job)
            time.sleep(self.check_interval)

    def dispatch(self, job):
        # CAS (Compare And Swap) via conditional UPDATE with WHERE status='QUEUED'
        # If the cancel API updates to CANCELLED between scan and UPDATE,
        # WHERE status='QUEUED' will not match, resulting in updated_rows=0, allowing skip
        updated_rows = db.execute("""
            UPDATE jobs SET status = 'DISPATCHING'
            WHERE namespace = :namespace
              AND job_id    = :job_id
              AND status    = 'QUEUED'
        """, namespace=job.namespace, job_id=job.job_id)

        if updated_rows == 0:
            # cancel API already updated to CANCELLED → skip
            return

        # Commit the CAS before creating the K8s Job.
        # This ensures that if create_job() raises an exception, rollback does not
        # revert DISPATCHING, and the WHERE status='DISPATCHING' condition in
        # subsequent mark_failed / increment_retry calls matches correctly.
        db.commit()

        # Proceed after DISPATCHING update is confirmed
        try:
            k8s.create_job(job)  # sets job.time_limit_seconds as activeDeadlineSeconds
            # AND status='DISPATCHING' condition prevents overwriting CANCELLED
            # If updated_rows == 0, status remains CANCELLED and
            # Watcher deletes the CANCELLED job's K8s Job in the next cycle (watcher.md §3 Step 5)
            db.update_status(
                job.namespace, job.job_id, "DISPATCHED", condition_status="DISPATCHING"
            )
        except TemporaryK8sError:
            # Retry handling from §2.2
            ...
        except PermanentK8sError:
            # AND status='DISPATCHING' condition prevents overwriting CANCELLED
            # updated_rows == 0 means cancel API already updated to CANCELLED, skip
            db.update_status(
                job.namespace, job.job_id, "FAILED", condition_status="DISPATCHING"
            )
        except Exception:
            # Even for uncaught exceptions like ValueError in build_k8s_job() or network errors,
            # transition the job to FAILED to prevent it from stalling in DISPATCHING
            db.update_status(
                job.namespace, job.job_id, "FAILED", condition_status="DISPATCHING"
            )
```

### 2.4 Gap Filling

#### 2.4.1 Background and Purpose

Kueue's `BestEffortFIFO` admits smaller subsequent jobs first when the head-of-line job cannot be admitted. Under the `preemption: Never` constraint, large jobs requiring an entire node may starve in environments where small jobs are continuously submitted.

To address this, the Dispatcher uses time_limit to perform gap filling in the time dimension. Space-based packing (node placement) is delegated to Kueue + K8s Scheduler, while the Dispatcher focuses exclusively on the control of "dispatching only jobs that fit in the gap until resources free up for large jobs."

#### 2.4.2 Detecting Stalled Jobs

Jobs that have remained in the DISPATCHED state for longer than `GAP_FILLING_STALL_THRESHOLD_SEC` (default 300 seconds = 5 minutes) are considered "stalled jobs."

```sql
SELECT namespace, job_id
FROM jobs
WHERE status = 'DISPATCHED'
  AND dispatched_at <= NOW() - MAKE_INTERVAL(secs => :threshold)
```

Stalled jobs represent "jobs passed to Kueue but not yet admitted due to insufficient resources." Normal jobs typically transition from DISPATCHED to RUNNING within seconds to tens of seconds, so exceeding the threshold indicates the job is waiting due to resource shortage.

If the threshold is too short, jobs currently being processed normally by Kueue may also be treated as stalled. If the threshold is too long, countermeasures are delayed. 5 minutes is a conservative value that accounts for normal cluster operation.

**Scope of stalling:** The impact of stalled jobs is limited to the same `(namespace, flavor)` unit. If a GPU flavor job stalls, dispatching of CPU flavor jobs in the same namespace is not restricted. Because resource pools are independent per flavor, resource shortage in one flavor does not affect dispatch in other flavors.

**Prerequisite:** This detection assumes stalling due to node resource shortage at the Kueue ClusterQueue level. DISPATCHED stalling due to insufficient namespace ResourceQuota is prevented by the ResourceQuota pre-check in §2.5. The operation assumes ResourceQuota is configured more loosely than dispatch_budget and ClusterQueue nominalQuota, so these limits normally take effect before ResourceQuota (see [resources.md](../architecture/resources.md) §1).

#### 2.4.3 Estimating Available Resources

When stalled jobs are detected, available capacity is estimated across two axes: time and resources.

##### Time-Based Estimation

Compute the "estimated remaining time until resources become available" from RUNNING jobs in the same `(namespace, flavor)`.

```sql
SELECT MIN(
  EXTRACT(EPOCH FROM
    (started_at + MAKE_INTERVAL(secs => time_limit_seconds)) - NOW()
  )
) AS min_remaining
FROM jobs
WHERE namespace = :namespace
  AND flavor = :flavor
  AND status = 'RUNNING'
  AND started_at IS NOT NULL
```

Subtract the current time from the latest completion time (started_at + time_limit_seconds) of all RUNNING jobs in the same `(namespace, flavor)`, and take the minimum as T. If T is negative, clamp it to 0. If no RUNNING jobs exist, return NULL (None).

T means "at least one RUNNING job in the same flavor will complete at least T seconds from now." In practice, jobs often complete before their time_limit, so T is a conservative (longer) estimate.

Reason for limiting scope to flavor level: CPU job completion does not free GPU resources, so referencing remaining time of RUNNING jobs in different flavors leads to unreasonable time estimates. For example, if a GPU flavor job stalls and only CPU jobs are RUNNING (5 minutes remaining), namespace-level estimation would yield T = 5 minutes, filtering GPU candidates based on CPU remaining time. With flavor-level estimation, T = None and the time condition is waived; only the resource condition applies.

##### Resource-Based Estimation

Estimate available resources in the ClusterQueue per flavor.

```
available[flavor] = flavor_quotas[flavor] - SUM(resource[flavor] of RUNNING jobs)
```

- Retrieve ClusterQueue nominalQuota from the `flavor_quotas` table (see [database.md](database.md) §7)
- Aggregate resources consumed by RUNNING jobs across the entire cluster by flavor
- DISPATCHED jobs are not included in the aggregation. Stalled jobs and other DISPATCHED jobs may not be admitted by Kueue, meaning they may not be consuming ClusterQueue resources
- For sweep jobs, resources are calculated as `parallelism` times
- Flavors with no rows in the `flavor_quotas` table are treated as having no limit

#### 2.4.4 Gap Filling Logic

For `(namespace, flavor)` pairs with stalled jobs, restrict dispatch candidates for QUEUED jobs in the same `(namespace, flavor)` based on both time and resources.

```
Dispatch cycle:
  1. Fetch candidates via the normal fetch_dispatchable_jobs()
  2. Check for stalled jobs per (namespace, flavor)
  3. (namespace, flavor) without stalling → pass candidates through as-is
  4. Estimate available ClusterQueue resources per flavor
  5. (namespace, flavor) with stalled jobs → filter candidates in the same (namespace, flavor):
     a. Compute the shortest remaining time T of RUNNING jobs in the same (namespace, flavor)
     b. T = None (no RUNNING jobs in the same (namespace, flavor)) → waive time condition (deadlock prevention)
     c. If T has a value → only jobs with time_limit_seconds ≤ T pass (time condition)
     d. Apply resource condition to jobs passing the time condition:
        - Compare with available resources for the candidate's flavor
        - Pass if CPU, memory, and GPU are all within available resources
        - Calculate sweep jobs as parallelism times
        - Deduct passed job resources from available resources (cumulative tracking)
     e. Jobs not meeting both conditions are held (re-evaluated next cycle)
```

**When no RUNNING jobs exist** (all jobs in the same `(namespace, flavor)` are waiting as DISPATCHED): T = None and the time condition is waived. The resource condition continues to apply. Dispatching when ClusterQueue has no capacity would not result in Kueue admission anyway, so maintaining the resource condition does not worsen deadlocks. If ClusterQueue has capacity, dispatch is allowed, and once a job transitions to RUNNING, T can be calculated in the next cycle and normal control resumes.

**Design decision: Limiting scope to (namespace, flavor)**

The impact of stalled jobs applies only within the same `(namespace, flavor)`, and does not restrict dispatching of other namespaces or other flavors in the same namespace. The reasons are as follows:

- Restricting other users' dispatch would allow a user submitting large jobs to block other users' execution, violating fairness
- Resource pools (ClusterQueue ResourceFlavor) are independent per flavor, so resource shortage in one flavor does not affect other flavors
- ClusterQueue-level resource management in Kueue is delegated to Kueue itself
- Within `(namespace, flavor)`, coordination is between jobs from the same user in the same resource pool, which is a reasonable control scope

**Design decision: Why filter downstream rather than modifying fetch_dispatchable_jobs**

Directly modifying the SQL query in `fetch_dispatchable_jobs` would require embedding the gap-filling logic in SQL, increasing complexity. Instead, filtering the fetched candidate list on the Python side is adopted. This provides:

- No impact on existing round-robin and budget control logic
- Gap filling can be toggled on/off via configuration
- Easier testing (the filtering function can be tested independently)

#### 2.4.5 Configuration Values

| Setting | ConfigMap Key | Default Value | Description |
|---|---|---|---|
| Stall detection threshold | `GAP_FILLING_STALL_THRESHOLD_SEC` | 300 (5 min) | Jobs in DISPATCHED state for longer than this many seconds are considered stalled |
| Gap filling enable/disable | `GAP_FILLING_ENABLED` | true | Set to false to skip gap-filling logic (legacy behavior) |

#### 2.4.6 Pseudocode

```python
# This is pseudocode for conceptual explanation.

def apply_gap_filling(
    session: Session,
    candidates: list[Job],
    settings: Settings,
) -> list[Job]:
    """Filter candidates for (namespace, flavor) pairs with stalled jobs."""
    if not settings.GAP_FILLING_ENABLED:
        return candidates

    # Fetch stalled jobs per (namespace, flavor)
    stalled = fetch_stalled_jobs(session, settings.GAP_FILLING_STALL_THRESHOLD_SEC)
    stalled_keys = {(job.namespace, job.flavor) for job in stalled}

    if not stalled_keys:
        return candidates

    # Estimate available ClusterQueue resources per flavor
    available = estimate_available_cluster_resources(session, settings)

    # Pass candidates for (namespace, flavor) without stalling
    result = [c for c in candidates if (c.namespace, c.flavor) not in stalled_keys]

    # Filter candidates for (namespace, flavor) with stalling
    for ns, flv in stalled_keys:
        key_candidates = [c for c in candidates if c.namespace == ns and c.flavor == flv]
        if not key_candidates:
            continue

        # Compute shortest remaining time of RUNNING jobs in the same (namespace, flavor)
        remaining = estimate_shortest_remaining(session, ns, flv)

        for c in key_candidates:
            # Time condition: waived if remaining=None (no RUNNING in same (namespace, flavor)) for deadlock prevention
            if remaining is not None and c.time_limit_seconds > remaining:
                logger.debug(
                    "Gap filling: holding %s/%d (time_limit=%ds > remaining=%ds)",
                    ns, c.job_id, c.time_limit_seconds, remaining,
                )
                continue

            # Resource condition: does the job fit within available resources for the flavor?
            multiplier = c.parallelism if c.completions is not None else 1
            job_cpu = c.cpu_millicores * multiplier
            job_mem = c.memory_mib * multiplier
            job_gpu = c.gpu * multiplier

            flavor_avail = available.get(c.flavor)
            if flavor_avail is not None:
                if (job_cpu > flavor_avail["cpu"]
                        or job_mem > flavor_avail["mem"]
                        or job_gpu > flavor_avail["gpu"]):
                    logger.debug(
                        "Gap filling: holding %s/%d (resource exceeds available for flavor=%s)",
                        ns, c.job_id, c.flavor,
                    )
                    continue
                # Cumulative tracking: deduct passed job resources
                flavor_avail["cpu"] -= job_cpu
                flavor_avail["mem"] -= job_mem
                flavor_avail["gpu"] -= job_gpu

            result.append(c)

    return result
```

#### 2.4.7 Constraints and Limitations

- **Time estimation accuracy**: This is a DB-based estimate and may diverge from the actual node availability as understood by Kueue/K8s Scheduler. If a job completes earlier than its time_limit, resources become available sooner than estimated, but the Dispatcher re-evaluates in the next cycle. Time estimation only references RUNNING jobs in the same `(namespace, flavor)`, so job completions in different flavors are not considered (reasonable because resource pools of different flavors are independent)
- **Resource estimation accuracy**: Since only RUNNING jobs are aggregated, resource consumption of recently DISPATCHED jobs admitted by Kueue but not yet transitioned to RUNNING is not reflected. This may result in a slight overestimate of available resources. While DRF scores add estimated consumption of DISPATCHING/DISPATCHED jobs via the in-flight CTE (see §1.2), gap-filling resource estimation is based on actual ClusterQueue consumption (RUNNING only), so a similar correction is not applied. Kueue makes the final admission decision, so there is no practical harm
- **Node placement is not considered**: Resource estimation is done using per-flavor totals, without considering individual node availability. If the total fits but a specific node has no capacity, Kueue may not admit the job
- **When time_limit_seconds significantly deviates from actual runtime**: If a user sets time_limit much longer than the actual runtime, T estimation becomes too conservative and the effectiveness of gap filling diminishes. However, this only biases control in the conservative direction (fewer dispatches) and does not worsen starvation

### 2.5 ResourceQuota Pre-check

The Dispatcher checks the remaining ResourceQuota for dispatch candidate namespaces and excludes jobs with insufficient resources from candidates (leaving them in QUEUED). This prevents jobs from stalling in DISPATCHED and ultimately failing with a timeout when User Pods such as JupyterHub are consuming ResourceQuota.

```python
# This is pseudocode for conceptual explanation.

candidates = fetch_dispatchable_jobs(session, settings)
candidates = apply_gap_filling(session, candidates, settings)
candidates = filter_by_resource_quota(session, candidates)  # added
```

`filter_by_resource_quota()` reads ResourceQuota information for candidate namespaces from the `namespace_resource_quotas` table (see [database.md](database.md) §8) and filters candidates with the following logic:

1. Jobs from namespaces with no rows in the table pass through without restriction
2. Iterate through candidates in DRF priority order; pass candidates with remaining resources (hard - used) ≥ job resource request
3. For sweep jobs, compute as `parallelism` times the resource request
4. Accumulate resources of passed jobs within the same cycle and reflect in remaining resource calculations for subsequent jobs (prevents over-dispatch within the same cycle)
5. If `hard_count` is not NULL, verify that remaining job count (hard_count - used_count - cumulative dispatch count within cycle) is ≥ 1. Sweep jobs are also counted as 1 K8s Job (no parallelism multiplier)

**Prerequisite:** ResourceQuota usage is periodically synchronized by the Watcher, so there is a delay of `RESOURCE_QUOTA_SYNC_INTERVAL_SEC` (default 10 seconds). This check is best-effort; ResourceQuota usage may change between the check and Kueue's admission. However, this is a significant improvement compared to no check (DISPATCHED stalling → timeout FAILED).

**Relationship with per-flavor budget separation:** Separating budget per `(namespace, flavor)` theoretically increases the maximum active jobs per namespace to `DISPATCH_BUDGET_PER_NAMESPACE × flavor count`. The `count/jobs.batch` pre-check (step 5) allows the Dispatcher to suppress dispatch before receiving K8s API errors when the total flavor-aware budget exceeds `count/jobs.batch`. If CPU/memory/GPU ResourceQuota was sized assuming a single budget, it may become relatively tight and more jobs may be rejected by ResourceQuota. This is conservative (fewer dispatches) and does not result in over-dispatch.

### 2.6 Startup Initialization

Upon Dispatcher restart, jobs stuck in `DISPATCHING` are reverted to `QUEUED`.

```python
def on_startup():
    db.reset_stale_dispatching_jobs()
    # UPDATE jobs SET status = 'QUEUED', retry_after = NULL WHERE status = 'DISPATCHING'
```

## 3. Sweep Job Dispatch

### 3.1 dispatch_budget Consumption Unit

1 sweep = 1 budget consumed. Regardless of `parallelism`, 1 row in the `jobs` table corresponds to 1 budget unit.

### 3.2 Building K8s Indexed Jobs

For sweep jobs (`jobs.completions IS NOT NULL`), `build_k8s_job` generates a K8s Job manifest with the following additional fields.

```yaml
spec:
  completionMode: Indexed
  completions: <completions>
  parallelism: <parallelism>
  backoffLimitPerIndex: 0
  activeDeadlineSeconds: <time_limit_seconds>
```

With `backoffLimitPerIndex: 0`, a task that fails once is immediately added to `failedIndexes` without retry. In sweep jobs, retrying failed tasks occupies parallelism slots and prevents other tasks from running, so the slot must be immediately freed.

For normal jobs, `backoffLimit: 0` is explicitly set so that failures result in immediate FAILED without retry. In research computation, jobs that error typically produce the same result on retry, and the disadvantage of increased Error Pods from retries (up to 7 by default) outweighs the benefit.

### 3.3 Command Wrapper

The sweep job command wrapper uses `CJOB_INDEX` export and indexed log directories.

```bash
export CJOB_INDEX=$JOB_COMPLETION_INDEX
LOG_DIR=/home/jovyan/.cjob/logs/{job_id}/$CJOB_INDEX
mkdir -p "$LOG_DIR"
exec > >(tee "$LOG_DIR/stdout.log") 2> >(tee "$LOG_DIR/stderr.log" >&2)
{user_command}
EXIT_CODE=$?
exec >&- 2>&-
wait
exit $EXIT_CODE
```

The only differences from the normal job wrapper are the addition of the `export CJOB_INDEX=$JOB_COMPLETION_INDEX` line and the index being included in `LOG_DIR`.

### 3.3.1 `_INDEX_` Placeholder

Replacement of `_INDEX_` with `$CJOB_INDEX` in user commands is performed on the CLI client side before sending to the Submit API (see [cli.md](cli.md) §3). The Dispatcher receives the already-replaced command string and embeds it directly in the command wrapper. This allows users to reference indices without being aware of shell variables, e.g., `cjob sweep -- python main.py --trial _INDEX_`. Within script files, the `$CJOB_INDEX` environment variable can also be referenced directly (since file contents are not subject to expansion by the user's shell).

### 3.4 Relationship with Gap Filling

The gap-filling logic operates as-is. The `time_limit_seconds` of a sweep job is the time limit for the entire sweep and is used in gap-filling estimation.

## 4. Job Scheduling Based on ResourceFlavor

Node assignment based on job flavor follows the same flow for both normal and sweep jobs.

1. The user specifies a flavor with `--flavor` (defaults to `DEFAULT_FLAVOR` if omitted)
2. The Submit API records it in `jobs.flavor`
3. The Dispatcher references `jobs.flavor` to create a K8s Job and submits it to Kueue's LocalQueue. For GPU jobs, the corresponding `gpu_resource_name` is added to the resource request (see §4.1). For all jobs, the flavor's `label_selector` (defined in the `RESOURCE_FLAVORS` setting) is set as the `nodeSelector` of the K8s Job
4. Kueue selects the flavor with `nodeLabels` matching the `nodeSelector` from the ClusterQueue's flavor list and schedules the job on a node

### 4.1 GPU Resource Configuration

When `job.gpu > 0`, `build_k8s_job` searches for the flavor definition matching `job.flavor` in the `RESOURCE_FLAVORS` setting and adds its `gpu_resource_name` (e.g., `nvidia.com/gpu`, `amd.com/gpu`) to the container's `resources.requests` and `resources.limits`. When `job.gpu == 0`, only CPU and memory are configured.

For ResourceFlavor definitions and configuration values, see [resources.md](resources.md) §ResourceFlavor and [kueue.md](kueue.md) §1.

### 4.2 CPU Limit Buffer

When `CPU_LIMIT_BUFFER_MULTIPLIER` (default `1.0`) is greater than `1.0`, `build_k8s_job` applies the multiplier to the CPU **limit only**. The request is not changed.

```yaml
# CPU_LIMIT_BUFFER_MULTIPLIER=1.05, --cpu 2
resources:
  requests:
    cpu: "2"       # unchanged (Kueue quota is calculated based on this)
  limits:
    cpu: "2100m"   # 2000m × 1.05 = 2100m
```

System processes inside the container (PID 1, bash, log output, etc.) consume a small amount of CPU, which may cause CFS throttling even when the user program uses only the requested amount. Adding a buffer to the limit mitigates this.

Since the request is not changed, there is no impact on Kueue quota consumption or DRF scheduling. When the multiplier is `1.0`, request == limit, which is the same as the conventional behavior (Guaranteed QoS). When the multiplier exceeds `1.0`, the QoS class changes to Burstable, but the practical impact is small for batch jobs managed by Kueue.
