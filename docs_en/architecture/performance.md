> *This document was auto-translated from the [Japanese original](../../docs/architecture/performance.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# Performance Analysis

## 1. Load Characteristics by Component

| Component | Processing | Dominant Load Factor |
|---|---|---|
| Submit API | Receiving `cjob add`, DB INSERT, flavor validation | Job submission frequency. Stateless, horizontally scalable (via replicas). Flavor validation only references DB (`node_resources` / `flavor_quotas`) and is lightweight |
| DB (PostgreSQL) | Read/write from all components (`jobs` / `namespace_daily_usage` / `node_resources` / `flavor_quotas` / `namespace_resource_quotas`, etc.) | Row count is small (hundreds to thousands), and indexes exist, so this rarely becomes an issue. Watcher resource sync adds UPSERTs, but frequency and row count are both minimal |
| Dispatcher | DB scan → DRF sort → gap-filling filter → ResourceQuota pre-check → K8s Job creation | Number of K8s API calls. Serial execution means each cycle is rate-limited by processing time. Gap-filling and ResourceQuota pre-check only access the DB and do not trigger additional external I/O. Budget is managed per `(namespace, flavor)`, but the additional SQL cost (one additional `ROW_NUMBER()`, GROUP BY expanding from `namespace` to `namespace, flavor` in the `active` CTE) is negligible since the row count is on the order of tens |
| Kueue | Admission decision → Pod scheduling | Rate-limited by Dispatcher's dispatch pace |
| Watcher | K8s Job status monitoring → DB updates, node resource sync, nominalQuota sync, ResourceQuota sync | Job monitoring: proportional to polling interval and number of active jobs. Resource sync: node and nominalQuota synced at `NODE_RESOURCE_SYNC_INTERVAL_SEC` (300s) intervals; ResourceQuota synced at `RESOURCE_QUOTA_SYNC_INTERVAL_SEC` (10s) intervals |

## 2. Bottleneck Analysis

### 2.1 Typical Research Workloads (Long-Running Job-Centric)

When jobs primarily run for tens of minutes to hours, the **Dispatcher** tends to be the bottleneck.

- K8s Job creation is executed serially one at a time via `dispatch_one` (each call takes hundreds of milliseconds to seconds)
- Maximum of `DISPATCH_BATCH_SIZE` (50) jobs per cycle
- Cycle interval `DISPATCH_BUDGET_CHECK_INTERVAL_SEC` (10 seconds)

At the current scale (approximately 10 concurrent active users), a throughput of 50 jobs/10 seconds is sufficient; even at 100 users, burst-time delays will be temporary and self-resolving (see §6.2).

**Improvement options (if needed):**

| Method | Effect | Trade-off |
|---|---|---|
| Increase `DISPATCH_BATCH_SIZE` | More jobs processed per cycle | Instantaneous load on K8s API increases |
| Reduce `DISPATCH_BUDGET_CHECK_INTERVAL_SEC` | Shorter cycle interval | DB and K8s API polling frequency increases |
| Parallelize K8s Job creation | Significantly improved throughput | Requires implementation changes; error handling becomes more complex |

### 2.2 High-Frequency Short-Duration Job Workloads

When jobs with a `time_limit` of a few minutes cycle rapidly in large numbers, the **Watcher** becomes the bottleneck.

Because the job lifecycle cycles quickly, RUNNING → SUCCEEDED transitions occur at high frequency. If the Watcher's state detection is delayed, the following cascade occurs:

```
Watcher detection delay (up to 10 seconds)
  → Jobs in DB remain as RUNNING
  → Dispatcher's budget calculation overestimates active job count
  → New jobs are not dispatched
  → Throughput degradation
```

**Improvement options (if needed):**

| Method | Effect | Trade-off |
|---|---|---|
| Reduce polling interval | Reduced detection delay (e.g., 3–5 seconds) | Increased load on K8s API |
| Migration to Watch API | Instant detection of state changes; also reduces K8s API load | Requires connection management implementation (reconnection, resourceVersion) |
| Adopting Informer pattern | Most efficient with Watch API + local cache | Complex implementation; Go (client-go) has more mature libraries than Python |

Switching languages (Python → Go, etc.) alone would have little effect. The bottleneck is I/O (K8s API polling interval and network latency), not CPU processing speed.

### 2.3 Watcher Resource Sync Overhead

In addition to job monitoring, the Watcher periodically executes the following K8s API calls (see [watcher.md](watcher.md) §1.1–§1.3).

| Sync Target | Interval | K8s API Calls | Data Volume |
|---|---|---|---|
| Node resources (`node_resources`) | 300 seconds | `list_node()` × number of flavors | flavor count × node count (approx. 10–50 entries) |
| nominalQuota (`flavor_quotas`) | 300 seconds | `get_cluster_custom_object()` × 1 | 1 ClusterQueue entry |
| ResourceQuota (`namespace_resource_quotas`) | 10 seconds | `list_namespace()` + `list_resource_quota_for_all_namespaces()` | namespace count + ResourceQuota count (approx. 20 each) |

Node resource and nominalQuota sync runs at 300-second intervals, so the load on the K8s API is nearly negligible. ResourceQuota sync runs at 10-second intervals (the same cycle as the Watcher's main loop) with 2 API calls, but response sizes are small (a list of tens of namespaces) and lightweight compared to the job monitoring `list_job_for_all_namespaces()`.

DB writes for all sync operations are UPSERTs (row count = node count, namespace count, or flavor count), around tens of rows, so load is negligible.

The likelihood of these sync operations becoming a bottleneck is extremely low at the current scale (approximately 10 users), and even at hundreds of users.

## 3. Watch API and Informer Pattern

### 3.1 Current Approach (Polling)

```
Watcher → K8s API: list_job_for_all_namespaces() (every 10 seconds)
K8s API → Watcher: full list of all Jobs (fetched in full every time)
```

All entries are fetched every time even if nothing changed, so response size grows as the number of active jobs increases.

### 3.2 Watch API

The K8s Watch API streams state-change events over an HTTP long connection.

```
Watcher → K8s API: watch (connect once)
K8s API → Watcher: "Job A is now RUNNING" (event, immediate)
K8s API → Watcher: "Job B is now Complete" (event, immediate)
```

- Instant detection of state changes (no polling delay)
- Significantly reduced load on K8s API (only diffs received)
- Recovery on connection drop (reconnect + re-list) must be implemented

Implementable using the Python `kubernetes` library's `watch.stream()`.

### 3.3 Informer Pattern

A pattern used by Kubernetes controllers and Prometheus. A superset of the Watch API.

```
1. On startup, fetch all entries via list → store in local cache
2. Receive diff events via Watch API → update cache
3. Logic operates against the cache (does not directly call K8s API)
4. On connection drop, automatically re-list + resume Watch
```

- Minimal K8s API load (initial list + Watch only thereafter)
- Local cache access has no network latency
- Go's `client-go` library has the most mature implementation. The Python `kubernetes` library has a simplified Informer implementation, but it is less mature

### 3.4 Comparison with Prometheus

Prometheus directly HTTP-scrapes the `/metrics` endpoint of each Pod, using the K8s API only for service discovery (fetching the Pod list). Prometheus does not impose significant load on the K8s API because metric collection targets are Pods rather than the K8s API, and it uses the Watch API (Informer pattern) for service discovery.

The information the Watcher needs (`status.conditions`, `status.active` of Jobs) exists only in the K8s API, so a Pod-direct query approach like Prometheus cannot be applied. The lesson from Prometheus is that "using the Watch API / Informer pattern can minimize load on the K8s API."

## 4. K8s Scalability Constraints

### 4.1 The Core Bottleneck

What limits CJob's scalability is not the Dispatcher or Watcher, but the **number of Job objects simultaneously existing on K8s**. Because each K8s Job stores one Job object + one Pod object in etcd, increasing the number of simultaneously existing objects causes the following issues:

| Factor | Impact | Horizontal Scalability |
|---|---|---|
| etcd write load | Job/Pod creation and state updates are all writes to etcd | Cannot be improved by adding nodes due to Raft consensus requirement |
| kube-controller-manager (Job controller) | Handles state transitions for all Jobs | Single leader; cannot scale out |
| Kueue controller | Handles admission decisions for all Workloads | Single leader; cannot scale out |
| kube-apiserver | Handles list/watch requests | Horizontally scalable by adding replicas |

While kube-apiserver can be addressed by adding replicas, etcd writes and single-leader controllers cannot scale out, making **the maximum number of simultaneously existing Jobs a structural constraint of K8s**.

### 4.2 Estimating Simultaneously Existing Job Count

The number of Job objects simultaneously existing on K8s is the sum of active Jobs and completed Jobs awaiting TTL expiration.

Since budget is applied per `(namespace, flavor)`, the maximum active Job count per user is `DISPATCH_BUDGET_PER_NAMESPACE × number of flavors`.

```
Simultaneous Job count = (concurrent active users × DISPATCH_BUDGET_PER_NAMESPACE × number of flavors)
                       + (completed Jobs within TTL window)
```

Shortening `ttlSecondsAfterFinished` (300 seconds = 5 minutes) reduces the backlog of completed Jobs, but does not affect active Job count. The table below estimates active Job count only; see §6.2 for the actual simultaneous count including TTL-pending completed Jobs.

**Theoretical maximum** (all users simultaneously using all flavors up to budget limit):

| Concurrent Active Users | Flavors | Budget/Flavor | Active Jobs (Max) | Safety |
|---|---|---|---|---|
| 10 | 2 | 32 | 640 | Comfortable |
| 20 | 2 | 32 | 1,280 | Comfortable |
| 50 | 2 | 32 | 3,200 | Comfortable |
| 100 | 2 | 32 | 6,400 | Risk of exceeding |
| 150 | 2 | 32 | 9,600 | Exceeds limit |

**Real-world expectation**: In research computing, the majority of users run only CPU jobs, and users simultaneously using GPU up to the budget limit are limited. If α represents the effective active coefficient per user (budget occupancy across all flavors), then effective active Job count = `user count × 32 × flavor count × α`. An α of 0.5–0.7 is a realistic expectation; with 2 flavors, the effective estimate per user is approximately 32–45 jobs. Additionally, ResourceQuota (`count/jobs.batch`) acts as a per-namespace safety valve, preventing dispatch from exceeding the theoretical limit (see [dispatcher.md](dispatcher.md) §2.5).

The practical upper limit for simultaneously existing Jobs in a standard K8s setup is approximately 5,000–10,000.

### 4.3 Improvement from Watch API Migration

Migrating to the Watch API eliminates the full-fetch `list_job_for_all_namespaces()` calls from the Watcher, significantly reducing read load on the API Server and etcd. However, the core bottleneck—etcd write load and single-leader controller processing capacity—is not improved.

Migration to the Watch API is expected to extend the maximum concurrent active user count by roughly 1.5x, but improvements of 2x or more should not be expected (see also §6.5 for combined effects).

### 4.4 Comparison with Supercomputer Job Schedulers

The reason schedulers like Slurm can handle large numbers of jobs is that the architecture is fundamentally different.

| | Supercomputer (Slurm, etc.) | CJob (K8s) |
|---|---|---|
| Overhead per job | 1 record in memory | Job + Pod objects in etcd |
| Execution start | Directly fork/exec process | Pod creation → container runtime startup |
| Scheduling | Scheduler directly assigns nodes | Dispatcher → K8s Job → Kueue → kube-scheduler → kubelet |
| Bulk task approach | Job array (1 entry = tens of thousands of tasks) | Indexed Job (used with `cjob sweep`, see §4.6). However, per-task overhead is larger than Slurm's job arrays |

In supercomputers, the per-job overhead is orders of magnitude smaller, so parameter sweeps of 1 core × 10,000 jobs are routine. K8s is designed as a general-purpose container orchestration system and is fundamentally ill-suited for workloads involving large numbers of short-lived jobs cycling rapidly.

### 4.5 Reducing etcd Load with 1-Job-N-Pod Configuration

K8s `batch/v1 Job` allows multiple Pods to be executed incrementally from a single Job object via the `completions` and `parallelism` fields. For example, with `completions: 100, parallelism: 10`, up to 10 Pods run simultaneously, and as each one completes, the next Pod starts, repeating until 100 total completions.

This can significantly reduce the number of Job objects in etcd (100 tasks represented as 1 Job instead of 100 Jobs). However, the following challenges exist:

| Challenge | Details |
|---|---|
| Command branching | All Pods share the same container spec; Indexed Job (`completionMode: Indexed`) must be used, with logic inside the Pod to branch commands based on the index |
| Failure isolation | If `backoffLimit` is reached, the entire Job fails; individual task success/failure cannot be handled independently |
| time_limit granularity | `activeDeadlineSeconds` applies to the entire Job; different time_limits cannot be set per task |
| Log separation | A mechanism to separate logs for multiple tasks within 1 Job is needed |
| Cancellation granularity | Individual tasks cannot be cancelled independently |
| Kueue admission | Kueue attempts to reserve resources for all `parallelism` Pods at once during admission, so incremental admission per Pod does not occur |

Due to these challenges, applying the 1-Job-N-Pod configuration generically is difficult; it is appropriate to limit it to groups of tasks with the same spec and same `time_limit`, such as parameter sweeps.

### 4.6 Load Reduction via Parameter Sweep Feature

The parameter sweep feature equivalent to supercomputer job arrays is implemented as `cjob sweep` (see [cli.md](cli.md) §3, [api.md](api.md) §2.1, [dispatcher.md](dispatcher.md) §3, [watcher.md](watcher.md) §4). It uses K8s Indexed Job (`completionMode: Indexed`) to run large numbers of small tasks with fewer Job objects.

**Realized benefits:**

- Reduction in Job objects in etcd (e.g., 1,000 tasks → 1 Job)
- Only 1 `dispatch_budget` consumed, allowing efficient use of budget slots
- Fewer Workloads submitted to Kueue, reducing admission processing load
- `backoffLimitPerIndex: 0` prevents individual task failures from propagating to the entire Job

**Performance characteristics:**

- For Indexed Jobs, the K8s Job controller creates Pods incrementally, so the Dispatcher only needs 1 K8s API call
- The Watcher retrieves `status.completedIndexes` / `status.failedIndexes` each polling cycle to update the DB. Even with many tasks, polling load is equivalent to regular jobs (just reading the status of 1 Job object)
- If the `parallelism` value is large, Kueue may attempt to reserve a large amount of resources at once, potentially increasing wait time until admission

**Incentive design:**

After introducing the sweep feature, reducing `MAX_QUEUED_JOBS_PER_NAMESPACE` and `DISPATCH_BUDGET_PER_NAMESPACE` creates an incentive to use sweep rather than individual submissions. With sweep, hundreds of tasks can be represented within a single submission slot, so even with stricter submission limits, the user's effective capacity does not decrease.

The order of introduction is important: the sweep feature must be implemented first, and submission limit reductions come after. Lowering limits before the sweep feature exists simply makes things inconvenient for users.

### 4.7 Scalability Improvement via dispatch_budget Reduction

In environments with many concurrent active users, reducing `DISPATCH_BUDGET_PER_NAMESPACE` can suppress the number of simultaneously existing Jobs.

In environments with many active users, it is not necessary for a single user to monopolize the entire cluster; fair sharing is the normal operational model. Therefore, lowering dispatch_budget does not mean a reduction in resource utilization efficiency.

However, during periods when active users are few, a low dispatch_budget may prevent a single user from fully utilizing the cluster, resulting in idle resources. This can be addressed with dynamic adjustment of dispatch_budget based on the number of active users, but this increases implementation complexity.

## 5. Current Recommendations

The current configuration (2 nodes, approximately 10 users) provides sufficient performance with the polling approach. In an operation where nodes are added proportionally to the number of users, computational resources themselves continue to scale, but there is an upper limit on the number of concurrent active users due to K8s structural constraints (see §6). Consider improvements when the following situations arise:

| Situation | Response |
|---|---|
| Dispatch of QUEUED jobs cannot keep up | Increase `DISPATCH_BATCH_SIZE`, reduce cycle interval |
| Short-job rotation is slow | Shorten Watcher polling interval, consider Watch API migration (§4.3) |
| Increasing number of concurrent active users | Lower `DISPATCH_BUDGET_PER_NAMESPACE` (§4.7), Watch API migration (§4.3) |
| Large number of small tasks (parameter sweep) | Use `cjob sweep` (§4.6, already implemented). Control load by adjusting completions / parallelism |
| Large jobs stall in Kueue (starvation) | Gap-filling feature handles this automatically (implemented, see [dispatcher.md](dispatcher.md) §2.4). Adjust detection threshold with `GAP_FILLING_STALL_THRESHOLD_SEC` |
| Jobs remain stuck as DISPATCHED due to insufficient ResourceQuota | ResourceQuota pre-check handles this automatically (implemented, see [dispatcher.md](dispatcher.md) §2.5). Adjust sync interval with `RESOURCE_QUOTA_SYNC_INTERVAL_SEC` |
| K8s API load becomes an issue | Consider adopting the Informer pattern (§3.3) |
| Want to understand cluster utilization | Grafana monitoring dashboard (see [monitoring.md](monitoring.md), already implemented). Visualizes CPU/GPU reservation rates, waiting job counts, and estimated wait times |

## 6. Scaling Estimates

### 6.1 Assumptions

| Item | Value |
|---|---|
| CPU per node | 128 cores |
| Memory per node | 500Gi |
| Current node count | 2 (approximately 10 users) |
| Node expansion policy | Increases proportionally to user count |
| Number of flavors | 2 (cpu, gpu) |
| DISPATCH_BUDGET_PER_NAMESPACE | 32 (applied per flavor) |
| DISPATCH_BATCH_SIZE | 50 |
| DISPATCH_ROUND_SIZE | 1 |
| DISPATCH_BUDGET_CHECK_INTERVAL_SEC | 10 seconds |
| ttlSecondsAfterFinished | 300 seconds (5 minutes) |
| count/jobs.batch (ResourceQuota) | 50 |
| FAIR_SHARE_WINDOW_DAYS | 7 days |
| GAP_FILLING_STALL_THRESHOLD_SEC | 300 seconds (5 minutes) |
| NODE_RESOURCE_SYNC_INTERVAL_SEC | 300 seconds (5 minutes) |
| RESOURCE_QUOTA_SYNC_INTERVAL_SEC | 10 seconds |

Since node count increases proportionally to user count, CPU and memory computational resources always scale. The analysis below focuses on structural constraints beyond resources.

### 6.2 Upper Limit Estimates by Bottleneck

#### K8s Simultaneous Job Count (Most Dominant Constraint)

As described in §4.1, what limits K8s scalability is the number of Job objects simultaneously existing. Adding nodes does not improve etcd write load or the single-leader controller-manager.

The simultaneous Job count is the sum of active Jobs and completed Jobs awaiting TTL expiration. With `ttlSecondsAfterFinished = 300s` (5 minutes), `count/jobs.batch = 50`, and 2 flavors:

```
Simultaneous Jobs/user = active Jobs + TTL-pending completed Jobs
Max active Jobs/user = DISPATCH_BUDGET_PER_NAMESPACE × number of flavors = 32 × 2 = 64
TTL-pending completed Jobs = completion rate × TTL = (active / avg execution time) × 300
```

Since budget is independent per flavor, the theoretical maximum active Job count per user is 64 (= 32 × 2 flavors). However, in research computing, the majority of users are CPU-job-centric, and cases where both CPU and GPU are simultaneously used up to budget limits are limited. The table below shows effective active counts by workload.

| Workload | Effective Active | TTL-pending | Total/User | At 100 Users |
|---|---|---|---|---|
| CPU only (avg 2h) | 32 | 1.3 | 33.3 | 3,330 |
| CPU only (avg 30m) | 32 | 5.3 | 37.3 | 3,730 |
| CPU + GPU mixed (avg 2h) | 48 (※1) | 2.0 | 50 → capped by Quota | 5,000 |
| Short CPU only (avg 5m) | 32 | 32 | 64 → capped by Quota 50 | 5,000 |

※1: Assumes CPU 32 + GPU 16. If all users use GPU up to budget limits, it can be 64, but since the number of GPU nodes is limited, in practice the CPU budget tends to fill up first.

When the majority of users are CPU-only, the impact of flavor-aware budget on scaling is limited. If the proportion of CPU + GPU mixed users is high, `count/jobs.batch` (ResourceQuota) acts as a safety valve to limit simultaneous Job count per namespace. When quota is reached, it naturally recovers as TTL expires, and the Dispatcher automatically recovers via retry.

**Watcher list load**: The `list_job_for_all_namespaces()` every 10 seconds retrieves up to 3,300–5,000 Jobs at 100 users (approximately 10–15MB/call at 1 Job ≈ 3KB). Large, but not at a level that would cause system failure. Solvable by migrating to Watch API (see §6.5). Additionally, there are ResourceQuota sync API calls (every 10 seconds: `list_namespace()` + `list_resource_quota_for_all_namespaces()`), but response sizes are metadata for tens of namespaces, orders of magnitude lighter than the Job list (see §2.3). Node resource and nominalQuota sync at 300-second intervals are negligible.

#### etcd write / kube-controller-manager

Job/Pod creation and state updates are all writes to etcd, and adding nodes does not help due to Raft consensus requirements. kube-controller-manager (Job controller) is also single-leader. However, in research computing (execution time of tens of minutes to hours), job creation and completion frequency is low, so write throughput is unlikely to become a bottleneck even at 100 users. With short-duration jobs (a few minutes) cycling in large numbers, impact may begin to appear at around 50 users.

#### Dispatcher Throughput

```
Max dispatch rate = DISPATCH_BATCH_SIZE / DISPATCH_BUDGET_CHECK_INTERVAL_SEC
                  = 50 jobs / 10 seconds = 5 jobs/sec = 300 jobs/min
```

In a burst scenario where all users submit simultaneously, the theoretical maximum is 100 users × 64 jobs (32 × 2 flavors) = 6,400 jobs, requiring approximately 21 minutes to complete dispatch. However, in practice with many CPU-only users, 100 users × 32–48 jobs = 3,200–4,800 jobs (approximately 11–16 minutes) is a realistic expectation. In the steady state of research computing, job completion and new submission frequency is gradual, so Dispatcher throughput becomes a bottleneck only during bursts, and delays are temporary and self-resolving.

The gap-filling filter ([dispatcher.md](dispatcher.md) §2.4) and ResourceQuota pre-check ([dispatcher.md](dispatcher.md) §2.5) are filtering processes on the candidate list in Python, without K8s API calls. The information needed for filtering (stalled jobs, remaining time for RUNNING jobs, available ClusterQueue resources, remaining ResourceQuota) is all retrieved from the DB. Gap-filling is scoped per `(namespace, flavor)`, so the `estimate_shortest_remaining` DB query runs for each `(namespace, flavor)` combination, but row count per query is around tens, and the number of combinations is limited to `stalled namespace count × flavor count` (typically a few to tens). The extension of the Dispatcher cycle from these additional processes is on the order of tens of milliseconds, negligible compared to K8s Job creation (hundreds of milliseconds/job). Moreover, by proactively avoiding dispatches that would certainly fail due to ResourceQuota insufficiency, there is a beneficial effect of reducing wasted K8s API calls.

When short-duration jobs cycle rapidly, Watcher detection delay (up to 10 seconds) causes overestimation of budget, making Dispatcher throughput appear lower (see §2.2).

#### PostgreSQL

The `jobs` table holds thousands to tens of thousands of rows, and the `idx_jobs_namespace_status` index makes Dispatcher scans efficient. Additional table row counts are as follows:

| Table | Estimated Row Count | Update Frequency |
|---|---|---|
| `namespace_daily_usage` | user count × window days (e.g., 200 × 7 = 1,400 rows) | On job RUNNING transition |
| `node_resources` | compute node count (10–50 rows) | Every 300 seconds |
| `flavor_quotas` | flavor count (2–5 rows) | Every 300 seconds |
| `namespace_resource_quotas` | user namespace count (20–200 rows) | Every 10 seconds |
| `namespace_weights` | namespaces with weight set (0–20 rows) | Only on admin operations |

All have extremely small row counts, and PostgreSQL is unlikely to become a bottleneck even at 200 users. The Dispatcher's DRF query (see [dispatcher.md](dispatcher.md) §1.2) includes a window aggregation of `namespace_daily_usage` and a SUM of `node_resources`, but the number of rows JOINed is on the order of namespace count (tens of rows), and the computational cost is negligible.

#### Kueue controller (Single Leader)

Kueue's admission decisions are processed by a single leader for all Workloads. When the number of simultaneously existing Workloads exceeds a few thousand, admission delays may occur. Using the sweep feature can dramatically reduce the number of Workloads (1,000 tasks → 1 Workload), so if sweep is assumed to be used, the impact is minimal.

### 6.3 Estimated User Count Upper Limits by Workload

Estimates for `DISPATCH_BUDGET_PER_NAMESPACE = 32` (per flavor), 2 flavors, `ttlSecondsAfterFinished = 300s`, and `count/jobs.batch = 50`.

| Workload | Estimated Upper Limit Users | Rate-Limiting Factor |
|---|---|---|
| CPU-only long-running jobs (30 min to hours) | **150–200** | K8s simultaneous Job count (approx. 5,000 at 150 users) |
| CPU + GPU mixed (long + sweep) | **100–130** | K8s simultaneous Job count (limited per namespace by Quota) |
| Short-duration job-centric (few minutes) | **80–100** | Watcher detection delay + Dispatcher throughput |

For CPU-only workloads, scalability equivalent to before flavor-aware budget introduction is maintained (active per user ≈ 33). For CPU + GPU mixed workloads, the theoretical maximum active count per user increases to 64, but `count/jobs.batch = 50` ResourceQuota acts as a per-namespace safety valve to control simultaneous Job count. In environments where a high proportion of users actively use GPU, the upper limit may drop to 100–130. Short-duration job limits depend on Watcher detection delay and do not change.

### 6.4 Scalability Extension via dispatch_budget Reduction

As described in §4.7, lowering `DISPATCH_BUDGET_PER_NAMESPACE` suppresses simultaneous Job count to accommodate more users. With the sweep feature available, the practical impact of lowering budget on users is minimal (see §4.6).

Since budget is applied per flavor, the theoretical maximum active Job count is `budget × flavor count × user count`. The table below shows theoretical limits with 2 flavors and real-world expectations when the majority are CPU-only users (α = 0.6).

| DISPATCH_BUDGET | 100 Users Active Jobs (Theoretical Max) | Same (Real-world α=0.6) | 200 Users (Theoretical Max) | Same (Real-world α=0.6) |
|---|---|---|---|---|
| 32 | 6,400 | 3,840 | 12,800 | 7,680 |
| 16 | 3,200 | 1,920 | 6,400 | 3,840 |
| 8 | 1,600 | 960 | 3,200 | 1,920 |

With budget 16, even at 200 users, the real-world expected active Job count is approximately 3,840, with comfortable margin against K8s practical limits even including TTL-pending completed Jobs. With budget 8, approximately 1,920 at 200 users leaves ample room, and simultaneous Job count is not an issue. Additionally, `count/jobs.batch` (ResourceQuota) acts as a per-namespace safety valve, limiting Job count per namespace before theoretical limits are reached.

However, when active users are few and budget is low, idle resources may occur. This can be addressed with dynamic adjustment based on active user count, but implementation complexity increases (see §4.7).

### 6.5 Combined Effect with Watch API Migration

Combining Watch API migration (§4.3) with dispatch_budget reduction further improves scalability.

The estimates below assume CPU-only long-running job-centric workloads (active per user ≈ 33). For CPU + GPU mixed workloads, the estimated upper limit decreases by 10–20% due to increased effective active count.

| Measure | Improvement Effect | Estimated Limit (CPU-only long-running job-centric) |
|---|---|---|
| Current (polling + budget 32/flavor) | - | 150–200 users |
| Watch API migration only | Reduced Watcher read load, eliminated detection delay | 200–250 users |
| budget 16/flavor only | Active Job count halved | 250–300 users |
| Watch API + budget 16/flavor | Both effects | 300–400 users |

At scales exceeding 400 users, etcd write load and single-leader controller processing capacity become structural limits. K8s cluster partitioning (multi-cluster) would be required.

### 6.6 Assumptions and Caveats for Estimates

- The estimates above are based on a worst-case scenario where all users are simultaneously active (submitting and running jobs). In practice, the active rate is often 50–80%, so effective upper limits are higher than these estimates
- Node specs assume 128 CPU cores / 500Gi Memory per node. Even if node specs differ, the bottleneck is K8s structural constraints, not computational resources, so there is no significant impact on upper limit estimates
- etcd performance depends on storage IOPS. If SSDs are not used, performance degradation may occur at lower scales than the estimates above
