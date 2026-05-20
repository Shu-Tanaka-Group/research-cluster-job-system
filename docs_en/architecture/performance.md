> *This document was auto-translated from the [Japanese original](../../docs/architecture/performance.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# Performance Analysis

## 1. Load Characteristics per Component

| Component | Processing | Dominant Load Factor |
|---|---|---|
| Submit API | Accepts `cjob add`, DB INSERT, flavor validation | Job submission frequency. Stateless and horizontally scalable (handled via replicas). Flavor validation only references the DB (`node_resources` / `flavor_quotas`) and is lightweight |
| DB (PostgreSQL) | Reads/writes from all components (`jobs` / `namespace_daily_usage` / `node_resources` / `flavor_quotas` / `namespace_resource_quotas`, etc.) | Row counts are small (hundreds to thousands), and indexes are in place, so this is unlikely to be problematic. UPSERTs from the Watcher's resource synchronization add load, but both frequency and row counts are minimal |
| Dispatcher | DB scan → DRF sort → gap-filling filter → ResourceQuota precheck → K8s Job creation | Number of K8s API calls. Since execution is serial, the cycle is bottlenecked by the time to process one cycle. Gap filling and ResourceQuota precheck only reference the DB and do not call the K8s API, so no additional external I/O is incurred. The budget is managed per `(namespace, flavor)`, but the additional SQL cost (one extra `ROW_NUMBER()`, GROUP BY of the `active` CTE going from `namespace` to `namespace, flavor`) is negligible since row counts are only a few dozen |
| Kueue | Admit decisions → Pod scheduling | Bottlenecked by the Dispatcher's dispatch pace |
| Watcher | Monitoring K8s Job state → DB updates, node resource sync, nominalQuota sync, ResourceQuota sync | Job monitoring: proportional to polling interval and number of active jobs. Resource sync: nodes and nominalQuota are synced every `NODE_RESOURCE_SYNC_INTERVAL_SEC` (300 seconds), and ResourceQuota is synced every `RESOURCE_QUOTA_SYNC_INTERVAL_SEC` (10 seconds) |

## 2. Bottleneck Analysis

### 2.1 Typical Research Compute Workload (Long-Running Jobs)

When jobs that run for tens of minutes to several hours dominate, the **Dispatcher** tends to become the bottleneck.

- K8s Job creation is executed serially one at a time by `dispatch_one` (several hundred milliseconds to a few seconds per call)
- Up to `DISPATCH_BATCH_SIZE` (50) items per cycle
- Cycle interval `DISPATCH_BUDGET_CHECK_INTERVAL_SEC` (10 seconds)

At the current scale (around 10 concurrently active users), throughput of 50 jobs / 10 seconds is sufficient. Even at a 100-user scale, temporary delays during bursts converge (see §6.2).

**Improvement options (if needed):**

| Method | Effect | Trade-off |
|---|---|---|
| Increase `DISPATCH_BATCH_SIZE` | More items processed per cycle | Increased instantaneous load on K8s API |
| Shorten `DISPATCH_BUDGET_CHECK_INTERVAL_SEC` | Shorter cycle interval | Increased polling frequency to DB and K8s API |
| Parallelize K8s Job creation | Significantly improves throughput | Requires implementation changes; error handling becomes more complex |

### 2.2 High-Frequency Short-Job Workload

When many jobs with a `time_limit` of a few minutes cycle rapidly, the **Watcher** becomes the bottleneck.

Because the job lifecycle cycles quickly, RUNNING → SUCCEEDED transitions occur frequently. If the Watcher's state detection lags, the following chain of effects occurs:

```
Watcher detection delay (up to 10 seconds)
  → Jobs remain RUNNING in the DB
  → Active job count is overestimated in the Dispatcher's budget calculation
  → New jobs are not dispatched
  → Throughput drops
```

**Improvement options (if needed):**

| Method | Effect | Trade-off |
|---|---|---|
| Shorten polling interval | Reduces detection delay (e.g., 3-5 seconds) | Increased load on K8s API |
| Migrate to Watch API | Detects state changes immediately. Also reduces K8s API load | Requires implementing connection management (reconnection, resourceVersion) |
| Adopt Informer pattern | Most efficient via Watch API + local cache | More complex implementation. Go (client-go) has more mature libraries than Python |

A language change (Python → Go, etc.) by itself has little effect. The bottleneck is not CPU processing speed but I/O (K8s API polling interval and network latency).

### 2.3 Watcher Resource Sync Overhead

In addition to job monitoring, the Watcher periodically calls the following K8s APIs (see [watcher.md](watcher.md) §1.1-§1.3):

| Sync Target | Interval | K8s API Calls | Data Volume |
|---|---|---|---|
| Node resources (`node_resources`) | 300 seconds | `list_node()` × number of flavors + `list_pod_for_all_namespaces()` × 1 (for DaemonSet reservation aggregation) | flavors × nodes + all Pods (a few hundred) |
| nominalQuota (`flavor_quotas`) | 300 seconds | `get_cluster_custom_object()` × 1 | 1 ClusterQueue |
| ResourceQuota (`namespace_resource_quotas`) | 10 seconds | `list_namespace()` + `list_resource_quota_for_all_namespaces()` | Number of namespaces + Number of ResourceQuotas (about 20 each) |

Since node resources and nominalQuota are synced every 300 seconds, the load on the K8s API is essentially negligible. The `list_pod_for_all_namespaces()` included in node resource sync has a relatively large response size because it retrieves all Pods in the cluster, but the impact is small because it runs only every 300 seconds and is used only for DaemonSet allocation calculation. ResourceQuota sync makes 2 API calls every 10 seconds (same cycle as the Watcher's main loop), but the response size is small (a list of dozens of namespaces) and is lightweight compared to `list_job_for_all_namespaces()` used for job monitoring.

In addition to the above periodic sync, the Watcher calls `list_namespaced_pod()` on demand at job state transition opportunities (RUNNING transition, terminal transition, sweep index changes) to retrieve the Pod's `node_name` and reflect it in the DB. Because this cost is proportional to the number of active jobs and the frequency of state transitions, it is not negligible for high-frequency short-job workloads (§2.2) and becomes one of the Watcher's load factors.

DB writes are all UPSERTs (row count = number of nodes, namespaces, or flavors), and load is negligible since only a few dozen rows are involved.

The likelihood of these sync processes becoming a bottleneck is extremely low not only at the current scale (about 10 users) but also at the hundreds-of-users scale.

### 2.4 Watcher Memory Consumption

The Watcher retains K8s API responses and DB query results in memory during each reconcile cycle. In particular, the results of `list_job_for_all_namespaces()` (a few tens of KB per Job) and `list_pod_for_all_namespaces()` (used for DaemonSet reservation aggregation) are the major factors that grow proportionally with cluster scale. Up to hundreds of items, a 256Mi memory limit is sufficient, but Jobs concurrently existing in excess of several thousand cause OOMKilled.

The following countermeasures control the Watcher's resident memory (see [watcher.md](watcher.md) §5 for details):

| Countermeasure | Target Hotspot | Effect |
|---|---|---|
| K8s Job pagination + lightweight dataclass conversion | `list_job_for_all_namespaces()` | Reduces memory per object to about 1/10, also suppresses parse-time peak |
| DaemonSet Pod per-page aggregation | `list_pod_for_all_namespaces()` (node_sync) | Avoids retaining Pod lists larger than the page size in memory at once |
| Narrowed DB query scope | DB reads in Step 2 / Step 8 | Read only Jobs corresponding to K8s Jobs, or only columns required for existence checks |
| Per-namespace batching of `list_namespaced_pod()` | `node_name` recording during reconcile | API calls scale with namespace count, not Job count. At most one `V1PodList` held at a time |

With these countermeasures, the Watcher's peak memory usage is expected to stay below 256Mi up to about 5,000 concurrently existing Jobs (in actual operation, the memory limit is set to 1Gi as an additional safety margin).

## 3. Watch API and Informer Pattern

### 3.1 Current Approach (Polling)

```
Watcher → K8s API: list_job_for_all_namespaces() (every 10 seconds)
K8s API → Watcher: List of all Jobs (full list each time)
```

Because all Jobs are retrieved every time even when there are no changes, the response size grows as the number of active jobs increases.

### 3.2 Watch API

The K8s Watch API streams state-change events over HTTP long connections.

```
Watcher → K8s API: watch (single connection)
K8s API → Watcher: "Job A became RUNNING" (event, immediate)
K8s API → Watcher: "Job B became Complete" (event, immediate)
```

- Detects state changes immediately (no polling delay)
- Significantly reduces load on K8s API (only differentials received)
- Requires implementing recovery (reconnect + re-list) on connection disconnect

Implementable with `watch.stream()` in Python's `kubernetes` library.

### 3.3 Informer Pattern

A pattern used by Kubernetes controllers and Prometheus. A superset of the Watch API.

```
1. At startup, retrieve all items via list → save to local cache
2. Receive differential events via Watch API → update cache
3. Logic operates on the cache (without hitting the K8s API directly)
4. On disconnect, automatically re-list and resume Watch
```

- Minimal load on K8s API (initial list + only Watch thereafter)
- No network latency for local cache access
- Go's `client-go` library has the most mature implementation. Python's `kubernetes` library also has a simple Informer implementation but is less mature

### 3.4 Comparison with Prometheus

Prometheus directly HTTP-scrapes the `/metrics` endpoint of each Pod, using the K8s API only for service discovery (retrieving the Pod list). The reason Prometheus does not place heavy load on the K8s API is that metric fetches go to each Pod (not the K8s API), and service discovery uses the Watch API (Informer pattern).

The information the Watcher needs (Job's `status.conditions`, `status.active`, `status.ready`) exists only in the K8s API, so Prometheus's approach of querying Pods directly cannot be applied. The lesson from Prometheus is: "Using the Watch API / Informer pattern can minimize the load on the K8s API."

## 4. K8s Scalability Constraints

### 4.1 Essence of the Bottleneck

What limits CJob's scalability is not the Dispatcher or Watcher, but the **number of Job objects that simultaneously exist on K8s**. Each K8s Job stores a Job object and a Pod object in etcd, so as the number of concurrently existing objects grows, the following become problems:

| Factor | Impact | Scale-out Feasibility |
|---|---|---|
| etcd write load | Job/Pod creation and state updates are all writes to etcd | Cannot be improved by adding nodes due to Raft consensus |
| kube-controller-manager (Job controller) | Processes state transitions for all Jobs | Cannot scale out due to single-leader |
| Kueue controller | Admission decisions for all Workloads | Cannot scale out due to single-leader |
| kube-apiserver | Processes list/watch requests | Horizontally scalable by adding replicas |

While kube-apiserver can be scaled by increasing replica count, etcd writes and single-leader controllers cannot scale out, so the **upper limit on concurrently existing Job count is a structural constraint of K8s**.

### 4.2 Estimating Concurrently Existing Job Count

The number of Job objects existing simultaneously on K8s is the sum of active Jobs and TTL-pending completed Jobs.

Since budget is per `(namespace, flavor)`, the max active Job count per user is `DISPATCH_BUDGET_PER_NAMESPACE × number of flavors`.

```
Concurrently existing Jobs = (concurrent active users × DISPATCH_BUDGET_PER_NAMESPACE × flavor count)
                           + (completed Jobs within TTL window)
```

Shortening `ttlSecondsAfterFinished` (300 seconds = 5 minutes) reduces the retention of completed Jobs but does not affect the active Job count. The table below shows estimates of active Jobs only; the actual concurrent existence including TTL-pending completed Jobs is analyzed in §6.2.

**Theoretical maximum** (when all users use all flavors up to the budget cap simultaneously):

| Concurrent active users | flavors | budget/flavor | active Jobs (max) | Safety |
|---|---|---|---|---|
| 10 | 2 | 32 | 640 | Comfortable |
| 20 | 2 | 32 | 1,280 | Comfortable |
| 50 | 2 | 32 | 3,200 | Comfortable |
| 100 | 2 | 32 | 6,400 | Risk of exceeding |
| 150 | 2 | 32 | 9,600 | Exceeds limit |

**Practical expectation**: In research computing, the majority of users run only CPU jobs, and users who saturate the GPU budget simultaneously are limited. If we let α be the effective active coefficient per user (budget occupancy across all flavors), effective active Jobs = `users × 32 × flavors × α`. α = 0.5-0.7 is realistic, and with 2 flavors, this works out to roughly 32-45 jobs per user. Additionally, ResourceQuota (`count/jobs.batch`) acts as a per-namespace safety valve, preventing dispatches that exceed the theoretical limit (see [dispatcher.md](dispatcher.md) §2.5).

In a standard K8s configuration, the practical upper limit is around 5,000-10,000 concurrently existing Jobs.

### 4.3 Improvement from Watch API Migration

Migrating to the Watch API eliminates the Watcher's full-list retrieval via `list_job_for_all_namespaces()`, significantly reducing the read load on API Server and etcd. However, the essential bottlenecks—etcd write load and the processing capacity of single-leader controllers—do not improve.

Watch API migration is expected to extend the upper limit of concurrent active users by about 1.5x, but improvements of 2x or more cannot be expected (see also §6.5 for combined effects).

### 4.4 Comparison with HPC Job Schedulers

The reason supercomputer schedulers such as Slurm can handle massive numbers of jobs is that their architectures are fundamentally different.

| | HPC (Slurm, etc.) | CJob (K8s) |
|---|---|---|
| Per-job overhead | 1 in-memory record | Job + Pod objects in etcd |
| Execution start | Direct fork/exec of process | Pod creation → container runtime startup |
| Scheduling | Scheduler directly assigns nodes | Dispatcher → K8s Job → Kueue → kube-scheduler → kubelet |
| Mass task mechanism | job array (1 record = tens of thousands of tasks) | Indexed Job (used by `cjob sweep`, see §4.6). However, per-task overhead is larger than Slurm's job array |

In supercomputers, the overhead per job is orders of magnitude smaller, so parameter sweeps of 1 core × 10,000 jobs are routinely executed. K8s is designed as a general-purpose container orchestration system and is inherently unsuited for use cases that rapidly cycle large numbers of short-lived jobs.

### 4.5 Reducing etcd Load with 1 Job N Pod Configuration

K8s's `batch/v1 Job` can execute multiple Pods in stages from a single Job object via the `completions` and `parallelism` fields. For example, with `completions: 100, parallelism: 10`, up to 10 Pods run simultaneously, and each time one completes, the next Pod starts, repeating until 100 complete in total.

This can significantly reduce the number of Job objects in etcd (100 tasks expressed as 1 Job rather than 100 Jobs). However, there are the following challenges:

| Challenge | Description |
|---|---|
| Command branching | Since all Pods share the same container spec, a mechanism is needed to use Indexed Job (`completionMode: Indexed`) and branch commands within the Pod by index |
| Failure isolation | If `backoffLimit` is reached, the entire Job becomes Failed. Cannot treat individual task success/failure independently |
| time_limit granularity | `activeDeadlineSeconds` applies to the entire Job. Cannot set a different time_limit per task |
| Log separation | A mechanism is needed to separate logs for multiple tasks within a single Job |
| Cancel granularity | Individual tasks cannot be canceled independently |
| Kueue admit | Since Kueue tries to reserve resources for the full `parallelism` at admit time, individual Pods cannot be admitted incrementally |

Due to these challenges, applying the 1 Job N Pod configuration generally is difficult, and limiting it to task groups with identical specs and identical time_limit (such as parameter sweeps) is reasonable.

### 4.6 Load Reduction via Parameter Sweep Feature

The parameter sweep feature, equivalent to HPC job arrays, is implemented as `cjob sweep` (see [cli.md](cli.md) §3, [api.md](api.md) §2.1, [dispatcher.md](dispatcher.md) §3, [watcher.md](watcher.md) §4). It uses K8s Indexed Job (`completionMode: Indexed`) to execute large numbers of small tasks with a smaller number of Job objects.

**Realized effects:**

- Reduces the number of Job objects in etcd (e.g., 1,000 tasks → 1 Job)
- Consumes only 1 `dispatch_budget` slot, so budget can be used efficiently
- Reduces the number of Workloads sent to Kueue, lightening admission processing
- `backoffLimitPerIndex: 0` prevents individual task failures from cascading to the entire Job

**Performance characteristics:**

- Since Indexed Job is created in stages by the K8s Job controller, the Dispatcher's K8s API call is only one
- The Watcher reads `status.completedIndexes` / `status.failedIndexes` each polling cycle and updates the DB. Even with a large number of tasks, polling load is on par with regular jobs (only reading the status of 1 Job object)
- If `parallelism` is large, Kueue tries to reserve a large amount of resources at once, which can lengthen the wait time until admit

**Incentive design:**

After introducing the sweep feature, lowering `MAX_QUEUED_JOBS_PER_NAMESPACE` and `DISPATCH_BUDGET_PER_NAMESPACE` creates an incentive to use sweep over individual submission. Since one submission slot in sweep can represent hundreds of tasks, even with stricter submission limits, the user's effective capacity does not decrease.

The order of introduction is important: the sweep feature must be implemented first, and submission limits lowered afterwards. Lowering the limits without the sweep feature would simply inconvenience users.

### 4.7 Scalability Improvement via dispatch_budget Reduction

In environments with a large number of concurrent active users, lowering `DISPATCH_BUDGET_PER_NAMESPACE` can suppress the number of concurrently existing Jobs.

In environments with many active users, there is no need for one user to monopolize the entire cluster; sharing fairly is the normal operational mode. Therefore, lowering dispatch_budget does not mean reduced resource utilization efficiency.

However, during times with few active users, a low dispatch_budget means a single user cannot fully utilize the cluster, leading to idle resources. This issue can be addressed by dynamic adjustment of dispatch_budget based on the active user count, but it adds implementation complexity.

## 5. Current Recommendations

In the current configuration (2 nodes, ~10 users), the polling approach provides sufficient performance. In an operation where node count grows in proportion to users, compute resources themselves continue to scale, but the structural constraints of K8s impose an upper limit on the number of concurrent active users (see §6). Improvements should be considered if the following situations arise:

| Situation | Response |
|---|---|
| QUEUED jobs cannot keep up with dispatching | Increase `DISPATCH_BATCH_SIZE`, shorten cycle interval |
| Short jobs cycle too slowly | Shorten Watcher polling interval, consider migrating to Watch API (§4.3) |
| Increase in concurrent active users | Lower `DISPATCH_BUDGET_PER_NAMESPACE` (§4.7), migrate to Watch API (§4.3) |
| Many small tasks (parameter sweep) | Use `cjob sweep` (§4.6, already implemented). Tune completions / parallelism to control load |
| Large jobs stall in Kueue (starvation) | Gap-filling feature handles this automatically (already implemented, see [dispatcher.md](dispatcher.md) §2.4). Adjust the detection threshold via `GAP_FILLING_STALL_THRESHOLD_SEC` |
| Jobs stagnate in DISPATCHED due to insufficient ResourceQuota | ResourceQuota precheck handles this automatically (already implemented, see [dispatcher.md](dispatcher.md) §2.5). Adjust the sync interval via `RESOURCE_QUOTA_SYNC_INTERVAL_SEC` |
| Jobs stagnate in DISPATCHED because they fit total quota but cannot be placed on any single node | per-node bin-packing precheck handles this automatically (already implemented, see [dispatcher.md](dispatcher.md) §2.6). Toggle on/off via `NODE_BIN_PACKING_ENABLED` |
| K8s API load becomes a problem | Consider adopting the Informer pattern (§3.3) |
| Want to understand cluster utilization | Grafana monitoring dashboard (see [monitoring.md](monitoring.md), already implemented). Visualizes CPU/GPU reservation rates, count of jobs waiting for resource allocation, and estimated wait times |

## 6. Scaling Estimates

### 6.1 Preconditions

| Item | Value |
|---|---|
| CPU per node | 128 cores |
| Memory per node | 500Gi |
| Current node count | 2 (about 10 users) |
| Node scaling policy | Increases proportionally to user count |
| Flavor count | 2 (cpu, gpu) |
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

Since node count scales with user count, compute resources (CPU/memory) always scale. The following analyzes structural constraints other than resources.

### 6.2 Upper Limit Estimates per Bottleneck

#### K8s Concurrent Job Count (most dominant constraint)

As described in §4.1, what limits K8s scalability is the number of concurrently existing Job objects. Adding nodes does not improve etcd write load or the single-leader controller-manager.

The number of concurrently existing Jobs is the sum of active Jobs and TTL-pending completed Jobs. With `ttlSecondsAfterFinished = 300s` (5 minutes), `count/jobs.batch = 50`, and 2 flavors:

```
Concurrent Jobs/user = active Jobs + TTL-pending completed Jobs
Max active Jobs/user = DISPATCH_BUDGET_PER_NAMESPACE × flavor count = 32 × 2 = 64
TTL-pending completed Jobs = completion rate × TTL = (active / avg run time) × 300
```

Since the budget is independent per flavor, the theoretical max active Job count per user is 64 (= 32 × 2 flavors). However, in research computing, the majority of users center on CPU jobs, and cases where both CPU and GPU are simultaneously used up to their budget caps are limited. The table below shows the effective active count by workload.

| Workload | Effective active | TTL-pending | Total/user | At 100 users |
|---|---|---|---|---|
| CPU only (avg 2h) | 32 | 1.3 | 33.3 | 3,330 |
| CPU only (avg 30m) | 32 | 5.3 | 37.3 | 3,730 |
| CPU + GPU mixed (avg 2h) | 48 (*1) | 2.0 | 50 → limited by Quota | 5,000 |
| Short CPU only (avg 5m) | 32 | 32 | 64 → limited by Quota 50 | 5,000 |

*1: Assumes CPU 32 + GPU 16. If all users use GPU up to the budget cap, it could be 64, but since GPU nodes are limited, in practice CPU budget tends to be saturated first.

When CPU-only users dominate, the impact of flavor-aware budget on scaling is limited. If the ratio of CPU + GPU mixed users is high, `count/jobs.batch` (ResourceQuota) acts as a safety valve, limiting the number of concurrently existing Jobs per namespace. When the quota is reached, recovery is natural via TTL expiration, and the Dispatcher's retry restores automatically.

**Watcher list load**: The number of Jobs retrieved by `list_job_for_all_namespaces()` every 10 seconds is at most 3,300-5,000 at 100 users (about 10-15MB per call assuming 1 Job ≈ 3KB). This is large but not at a level that breaks operation. Resolvable via Watch API migration (see §6.5). In addition, ResourceQuota sync (`list_namespace()` + `list_resource_quota_for_all_namespaces()` every 10 seconds) involves API calls, but the response size is metadata for dozens of namespaces and is orders of magnitude lighter than the Job list (see §2.3). Node resource and nominalQuota syncs run every 300 seconds and are negligible.

#### etcd write / kube-controller-manager

Job/Pod creation and state updates are all writes to etcd and cannot improve by adding nodes due to Raft consensus. kube-controller-manager (Job controller) is also single-leader. However, for research computing (with run times of tens of minutes to several hours), the frequency of job creation and completion is low, so write throughput is unlikely to become a bottleneck even at 100 users. With large numbers of short jobs (few minutes) cycling rapidly, impact starts to appear from around 50 users.

#### Dispatcher Throughput

```
Max dispatch rate = DISPATCH_BATCH_SIZE / DISPATCH_BUDGET_CHECK_INTERVAL_SEC
                  = 50 / 10s = 5/s = 300/min
```

In a burst scenario where all users submit at once, the theoretical max is 100 users × 64 (32 × 2 flavors) = 6,400 jobs, which takes about 21 minutes to dispatch. In actual operation, however, CPU-only users dominate and a realistic expectation is 100 users × 32-48 = 3,200-4,800 jobs (about 11-16 minutes). In the steady state of research computing, job completion and new submissions occur at a gentle pace, so Dispatcher throughput becomes a bottleneck only during bursts, and resolves with temporary delay.

The gap-filling filter ([dispatcher.md](dispatcher.md) §2.4) and ResourceQuota precheck ([dispatcher.md](dispatcher.md) §2.5) are operations that filter the candidate list on the Python side and do not involve K8s API calls. The information needed for filtering (stalled jobs, remaining time of RUNNING jobs, ClusterQueue available resources, ResourceQuota remaining resources) is all retrieved from the DB. Since gap filling is scoped per `(namespace, flavor)`, the `estimate_shortest_remaining` DB query runs for each combination of `(namespace, flavor)`, but the row count per query is only a few dozen, and the combination count is limited to `number of stalled namespaces × number of flavors` (typically a few to a few dozen). The extension of the Dispatcher cycle due to these additional processes is only a few tens of milliseconds, negligible compared to K8s Job creation (a few hundred milliseconds per item). Rather, they have the effect of avoiding wasted K8s API calls by preventing dispatches that would certainly fail due to insufficient ResourceQuota.

When short jobs cycle rapidly, Watcher detection delay (up to 10 seconds) causes overestimation of budget, leading to apparent throughput degradation of the Dispatcher (see §2.2).

#### PostgreSQL

The `jobs` table has only a few thousand to tens of thousands of rows, and the `idx_jobs_namespace_status` index makes Dispatcher scans efficient. Row counts for additional tables are as follows.

| Table | Estimated rows | Update frequency |
|---|---|---|
| `namespace_daily_usage` | users × flavors × window days (e.g., 200 × 2 × 7 = 2,800 rows) | At Job RUNNING transition. For short jobs that complete without being observed as RUNNING, recorded as fallback at SUCCEEDED/FAILED transitions |
| `node_resources` | Compute nodes (10-50 rows) | Every 300 seconds |
| `flavor_quotas` | flavors (2-5 rows) | Every 300 seconds |
| `namespace_resource_quotas` | User namespaces (20-200 rows) | Every 10 seconds |
| `namespace_weights` | Namespaces with explicit weight (0-20 rows) | Only on admin operations |

All have extremely few rows, and PostgreSQL is unlikely to be a bottleneck even at 200 users. The Dispatcher's DRF query (see [dispatcher.md](dispatcher.md) §1.2) includes window aggregation of `namespace_daily_usage` per `(namespace, flavor)` and a per-flavor SUM of `node_resources`, but the joined row count is on the order of `namespace × flavor` (a few dozen to a few hundred rows), and the computational cost is negligible.

#### Kueue Controller (single-leader)

Kueue's admission decisions are processed by a single leader for all Workloads. If the number of concurrently existing Workloads exceeds several thousand, admission delays may occur. Since use of the sweep feature can substantially reduce the Workload count (1,000 tasks → 1 Workload), if sweep is assumed to be used in combination, impact is minor.

### 6.3 Estimated User Limits per Workload

Estimates with `DISPATCH_BUDGET_PER_NAMESPACE = 32` (per flavor), 2 flavors, `ttlSecondsAfterFinished = 300s`, `count/jobs.batch = 50`.

| Workload | Estimated user limit | Limiting factor |
|---|---|---|
| CPU-only long-job centric (30 minutes to several hours) | **150-200** | K8s concurrent Job count (~5,000 at 150 users) |
| CPU + GPU mixed (long + sweep) | **100-130** | K8s concurrent Job count (limited per namespace by Quota) |
| Short-job centric (few minutes) | **80-100** | Watcher detection delay + Dispatcher throughput |

For CPU-only workloads, scalability is maintained on par with before flavor-aware budget (active ≈ 33 per user). For CPU + GPU mixed workloads, the theoretical max active count per user rises to 64, but the `count/jobs.batch = 50` ResourceQuota functions as a per-namespace safety valve, so concurrent Job counts remain controlled. In environments where the ratio of users actively using GPU is high, the limit may drop to 100-130. The limit for short jobs depends on Watcher detection delay and does not change.

### 6.4 Scalability Expansion via dispatch_budget Reduction

As described in §4.7, lowering `DISPATCH_BUDGET_PER_NAMESPACE` suppresses the number of concurrently existing Jobs and accommodates more users. With the sweep feature, the practical impact of lowering the budget on users is minor (see §4.6).

Since budget is applied per flavor, the theoretical max active Job count is `budget × flavors × users`. The table below shows the theoretical limits and the realistic expectation when CPU-only users dominate (α = 0.6) for the case of 2 flavors.

| DISPATCH_BUDGET | Active Jobs at 100 users (theoretical) | Same (realistic α=0.6) | At 200 users (theoretical) | Same (realistic α=0.6) |
|---|---|---|---|---|
| 32 | 6,400 | 3,840 | 12,800 | 7,680 |
| 16 | 3,200 | 1,920 | 6,400 | 3,840 |
| 8 | 1,600 | 960 | 3,200 | 1,920 |

With budget 16, realistic active Job count is about 3,840 even at 200 users, and including TTL-pending Jobs at 300s TTL, there is room compared to K8s's practical upper limit. With budget 8, even 200 users have about 1,920, well within margin, and concurrent Job count is not an issue. Furthermore, `count/jobs.batch` (ResourceQuota) functions as a per-namespace safety valve, limiting Job count per namespace before reaching the theoretical limit.

However, during times with few active users, a low budget causes idle resources. This issue can be addressed via dynamic adjustment based on active user count, but adds implementation complexity (see §4.7).

### 6.5 Combined Effect with Watch API Migration

Combining Watch API migration (§4.3) with dispatch_budget reduction further improves scalability.

The following estimates assume a CPU-only long-job centric workload (active ≈ 33 per user). For CPU + GPU mixed workloads, the increase in effective active count lowers the estimates by 10-20%.

| Measure | Improvement effect | Estimated limit (CPU-only long-job centric) |
|---|---|---|
| Current (polling + budget 32/flavor) | - | 150-200 |
| Watch API migration only | Reduces Watcher read load, eliminates detection delay | 200-250 |
| budget 16/flavor only | Halves active Job count | 250-300 |
| Watch API + budget 16/flavor | Both effects | 300-400 |

Beyond 400, etcd write load and the processing capacity of single-leader controllers become structural limits. K8s cluster partitioning (multi-cluster) becomes necessary.

### 6.6 Assumptions and Caveats

- The estimates above are based on a worst-case scenario where all users are concurrently active (submitting and running jobs). In practice, the active rate is often 50-80%, so effective limits are higher than these estimates
- Node specs assume CPU 128 cores / Memory 500Gi per node. Even with different node specs, since the bottleneck is structural K8s constraints rather than compute resources, the impact on the upper limit estimates is small
- etcd performance depends on storage IOPS. Without SSDs, performance degradation may occur at lower thresholds than estimated above
