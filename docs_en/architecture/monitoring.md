> *This document was auto-translated from the [Japanese original](../../docs/architecture/monitoring.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# Monitoring Design

## 1. Overview

A Grafana dashboard is provided for users to check the utilization status of the CJob cluster.

**Dashboard purpose**: Enable users to determine at a glance "Is the cluster busy right now? Should I submit a job or wait?"

**Data sources**:
- **Prometheus**: Node/Pod metrics, Kueue metrics, CJob application metrics
- **PostgreSQL**: CJob DB (job status, actual wait times)

## 2. Prerequisites

### 2.1 Enabling Kueue ClusterQueue Resource Metrics

In Kueue v0.16.4, ClusterQueue resource usage metrics (`kueue_cluster_queue_resource_usage` / `kueue_cluster_queue_nominal_quota`) are disabled by default. These are required for the CPU/GPU utilization gauges in the dashboard, so they must be enabled.

Add the following to `controller_manager_config.yaml` in the `kueue-manager-config` ConfigMap:

```yaml
metrics:
  enableClusterQueueResources: true
```

After the change, restart the kueue-controller-manager Pod:

```bash
kubectl rollout restart deployment kueue-controller-manager -n kueue-system
```

### 2.2 Adding PostgreSQL Data Source to Grafana

Create a read-only user in CJob's PostgreSQL and register it as a data source in Grafana.

#### Creating a Read-Only User

```sql
CREATE ROLE grafana_reader LOGIN PASSWORD '<secure-password>';
GRANT CONNECT ON DATABASE cjob TO grafana_reader;
GRANT USAGE ON SCHEMA public TO grafana_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_reader;
```

#### Grafana Data Source Settings

| Field | Value |
|---|---|
| Name | CJob DB |
| Type | PostgreSQL |
| Host | `postgres.cjob-system.svc.cluster.local:5432` |
| Database | `cjob` |
| User | `grafana_reader` |
| TLS/SSL Mode | Configure according to your environment |

### 2.3 Verifying CJob Application Metrics Scraping

The Submit API and Watcher expose Prometheus counter metrics. Configure scraping via Prometheus Operator's ServiceMonitor / PodMonitor.

| Component | Port | Path | Notes |
|---|---|---|---|
| Submit API | 8080 | `/metrics` | Same port as the FastAPI application |
| Watcher | 9090 (`WATCHER_METRICS_PORT`) | `/metrics` | Served on a separate thread from the main loop |
| Dispatcher | 9090 (`DISPATCHER_METRICS_PORT`) | `/metrics` | Served on a separate thread from the main loop |

**Exposed metrics**:

| Metric name | Type | Labels | Instrumentation point | Description |
|---|---|---|---|---|
| `cjob_jobs_submitted_total` | Counter | — | Submit API | Number of jobs submitted (incremented on successful `submit_job` / `submit_sweep`) |
| `cjob_jobs_completed_total` | Counter | `status` | Watcher / Submit API | Number of completed jobs. `status` is `succeeded` / `failed` / `cancelled` |

Instrumentation points for `cjob_jobs_completed_total`:
- `succeeded` / `failed`: On status transition in Watcher's `reconcile_cycle()`
- `failed` (K8s Job disappeared): On job disappearance detection in Watcher's `reconcile_cycle()`
- `failed` (dispatch failure): In Dispatcher's `mark_failed()` on permanent errors or retry limit exceeded
- `cancelled`: On successful cancellation in Submit API's `cancel_single()`

These counters reset on process restart, but this has no impact on the dashboard since Prometheus's `increase()` / `rate()` functions handle resets automatically.

### 2.4 Verifying Kueue Prometheus Metrics Scraping

Verify that Kueue's Prometheus metrics are being scraped via ServiceMonitor or PodMonitor. When installing Kueue via Helm, a ServiceMonitor is created with `enablePrometheus: true`. For manifest-based installations, configure manually by referring to:

```bash
kubectl apply --server-side -f https://github.com/kubernetes-sigs/kueue/releases/download/v0.16.4/prometheus.yaml
```

## 3. Dashboard Design

### 3.1 Basic Information

| Field | Value |
|---|---|
| Title | CJob Cluster Status |
| Default time range | 6 hours |
| Auto-refresh interval | 30 seconds |
| Tags | `cjob` |

### 3.2 Panel Layout

#### Row 1: Summary (Traffic Light)

A summary row placed at the top of the dashboard. Allows understanding cluster status in 5 seconds.

| Panel | Type | DataSource | Content | Thresholds |
|---|---|---|---|---|
| CPU Reservation Rate | Gauge | Prometheus | CPU reservation rate for cpu flavor (total job requests / quota limit) | green < 60%, yellow < 85%, red >= 85% |
| CPU Sub Reservation Rate | Gauge | Prometheus | CPU reservation rate for cpu-sub flavor (total job requests / quota limit) | green < 60%, yellow < 85%, red >= 85% |
| GPU Reservation Rate | Gauge | Prometheus | GPU reservation rate for gpu flavor (total job requests / quota limit) | green < 50%, yellow < 75%, red >= 75% |
| Jobs Awaiting Resource Allocation | Stat | PostgreSQL | Number of jobs in DB waiting for resource allocation (DISPATCHED) | green < 5, yellow < 20, red >= 20 |
| Resource Allocation Wait (P50) | Stat | Prometheus | Median Kueue admission wait time (last 1 hour) | green < 60s, yellow < 300s, red >= 300s |

#### Row 2: Current Job Status

| Panel | Type | DataSource | Content |
|---|---|---|---|
| Job Status Breakdown | Pie chart | PostgreSQL | Job status breakdown for the last 24 hours (excluding DISPATCHING) |
| Running Jobs | Stat | PostgreSQL | Total number of running jobs across all users |
| Success Rate (Last 24 hours) | Stat | Prometheus | SUCCEEDED / (SUCCEEDED + FAILED) |
| Active Users | Stat | PostgreSQL | Number of users (namespaces) with RUNNING jobs |
| Cluster Node Count | Stat | PostgreSQL | Number of records in the node_resources table |

#### Row 3: Queue Status

| Panel | Type | DataSource | Content |
|---|---|---|---|
| Queue Usage by Flavor | Table | PostgreSQL | Number of running, awaiting resource allocation, submitted, and held jobs per flavor |
| Queue Job Count Over Time | Time series | Prometheus | Trend of running (admitted_active) and awaiting-resource-allocation (pending) jobs |
| Job Submission and Completion Over Time | Time series (line) | Prometheus | Submission and completion counts by time period |

#### Row 4: CPU Flavor Details

| Panel | Type | DataSource | Content |
|---|---|---|---|
| CPU Reservation Over Time | Time series | Prometheus | CPU reservation vs quota limit for cpu flavor |
| Memory Reservation Over Time | Time series | Prometheus | Memory reservation vs quota limit for cpu flavor (displayed in GiB) |

#### Row 5: GPU Flavor Details

| Panel | Type | DataSource | Content |
|---|---|---|---|
| GPU Reservation Over Time | Time series | Prometheus | GPU reservation vs quota limit for gpu flavor |
| GPU Node CPU Reservation | Time series | Prometheus | CPU reservation vs quota limit for gpu flavor |
| GPU Node Memory Reservation | Time series | Prometheus | Memory reservation vs quota limit for gpu flavor (displayed in GiB) |

#### Row 6: CPU Sub Flavor Details

| Panel | Type | DataSource | Content |
|---|---|---|---|
| CPU Reservation Over Time | Time series | Prometheus | CPU reservation vs quota limit for cpu-sub flavor |
| Memory Reservation Over Time | Time series | Prometheus | Memory reservation vs quota limit for cpu-sub flavor (displayed in GiB) |

#### Row 7: Wait Time Analysis

| Panel | Type | DataSource | Content |
|---|---|---|---|
| Resource Allocation Wait Time Over Time (P50 / P95) | Time series | Prometheus | Percentile trend of Kueue admission wait time |
| Recent Job Wait Times | Table | PostgreSQL | Actual wait times for jobs in the last 6 hours (started_at - created_at) |

#### Row 8: Hourly Trends

| Panel | Type | DataSource | Content |
|---|---|---|---|
| Hourly Congestion (7-day average) | Bar chart | Prometheus | Average job submissions per hour from 0-23. Allows targeting less busy periods for job submission |

### 3.3 Metric Units (Kueue v0.16.4)

| Resource | Metric unit | Display unit | Conversion |
|---|---|---|---|
| CPU | Cores (float64) | Cores | No conversion needed |
| Memory | Bytes (float64) | GiB | `/ 1024 / 1024 / 1024` |
| GPU | Count (float64) | Count | No conversion needed |

In Kueue v0.16.4, values are converted using `resource.Quantity.AsApproximateFloat64()`, so memory is reported in bytes (e.g., `nominalQuota: "1000Gi"` → `1073741824000`).

Utilization gauges (Row 1) are ratios of usage / quota, so the numerator and denominator are in the same units and cancel out — no conversion needed.

### 3.4 Job Status Display Policy

Because the dashboard is user-facing, raw `jobs.status` values are not displayed directly. Instead they are translated as follows.

| Internal status | UI label | Notes |
|---|---|---|
| QUEUED | Submitted | The job has been submitted to cjob and is waiting for Dispatcher processing |
| DISPATCHING | (not displayed) | A transient state that usually lasts less than a second. Stalls in this state are not a user-facing indicator; they are detected by administrator-facing Prometheus alerts |
| DISPATCHED | Awaiting Resource Allocation (abbreviated as "Allocation Wait" in the pie chart) | Registered with K8s/Kueue and waiting for admission. Runs as soon as capacity becomes available in the ClusterQueue's nominalQuota |
| RUNNING | Running | |
| HELD | Held | A QUEUED job that has been paused by the user with `cjob hold` |
| SUCCEEDED / FAILED / CANCELLED / DELETING | Succeeded / Failed / Cancelled / Deleting | |

Statuses counted by each panel:

| Panel | Counted statuses | Purpose |
|---|---|---|
| Jobs Awaiting Resource Allocation (Row 1) | DISPATCHED only | Immediate indicator of resource contention |
| Job Status Breakdown pie chart (Row 2) | QUEUED / DISPATCHED / RUNNING / HELD / terminal states | 24-hour overview (only DISPATCHING is excluded) |
| Queue Usage by Flavor (Row 3) | QUEUED / DISPATCHED / RUNNING / HELD | Breakdown of queue state per flavor |
| Queue Job Count Over Time (Row 3) | `kueue_admitted_active_workloads` and `kueue_pending_workloads` (≒ DISPATCHED) | Trend of resource contention |

**Reason for excluding QUEUED from resource-contention indicators (Row 1 / Row 3 time series)**:

"Jobs Awaiting Resource Allocation" and "Queue Job Count Over Time" represent contention against the ClusterQueue's nominalQuota, so they count DISPATCHED only. QUEUED jobs are processed per namespace by the Dispatcher, and another user's QUEUED jobs do not directly block the current user's execution, so they are not included in the cluster-wide congestion indicators.

**Reason for including QUEUED / HELD in diagnostic views (Queue Usage by Flavor / Job Status Breakdown pie chart)**:

These panels are not resource-contention indicators but diagnostic breakdown views showing how jobs are distributed across states. Queue Usage by Flavor is a table that provides a per-flavor overview of queue usage; if HELD is displayed then QUEUED must also be displayed to stay consistent with the job lifecycle. The Job Status Breakdown pie chart is an overview indicator of the distribution of all jobs in the last 24 hours. Both exclude only DISPATCHING (because it is a transient state).

## 4. Key Queries

### 4.1 PromQL

```promql
# Success rate (last 24 hours)
sum(increase(cjob_jobs_completed_total{status="succeeded"}[24h]))
/
sum(increase(cjob_jobs_completed_total{status=~"succeeded|failed"}[24h]))
* 100

# Job submission trend (10-minute intervals)
sum(increase(cjob_jobs_submitted_total[10m]))

# Job completion trend (10-minute intervals)
sum(increase(cjob_jobs_completed_total[10m]))

# Hourly congestion (7-day average, panel time range: 24h)
(
  sum(increase(cjob_jobs_submitted_total[1h]))
  + (sum(increase(cjob_jobs_submitted_total[1h] offset 1d)) or vector(0))
  + (sum(increase(cjob_jobs_submitted_total[1h] offset 2d)) or vector(0))
  + (sum(increase(cjob_jobs_submitted_total[1h] offset 3d)) or vector(0))
  + (sum(increase(cjob_jobs_submitted_total[1h] offset 4d)) or vector(0))
  + (sum(increase(cjob_jobs_submitted_total[1h] offset 5d)) or vector(0))
  + (sum(increase(cjob_jobs_submitted_total[1h] offset 6d)) or vector(0))
) / 7
```

```promql
# CPU utilization gauge
kueue_cluster_queue_resource_usage{cluster_queue="cjob-cluster-queue", flavor="cpu", resource="cpu"}
/
kueue_cluster_queue_nominal_quota{cluster_queue="cjob-cluster-queue", flavor="cpu", resource="cpu"}

# CPU Sub utilization gauge
kueue_cluster_queue_resource_usage{cluster_queue="cjob-cluster-queue", flavor="cpu-sub", resource="cpu"}
/
kueue_cluster_queue_nominal_quota{cluster_queue="cjob-cluster-queue", flavor="cpu-sub", resource="cpu"}

# GPU utilization gauge
kueue_cluster_queue_resource_usage{cluster_queue="cjob-cluster-queue", flavor="gpu-a100", resource="nvidia.com/gpu"}
/
kueue_cluster_queue_nominal_quota{cluster_queue="cjob-cluster-queue", flavor="gpu-a100", resource="nvidia.com/gpu"}

# Wait time P50 (last 1 hour)
histogram_quantile(0.5, rate(kueue_admission_wait_time_seconds_bucket{cluster_queue="cjob-cluster-queue"}[1h]))

# Wait time P95 (last 30 minutes)
histogram_quantile(0.95, rate(kueue_admission_wait_time_seconds_bucket{cluster_queue="cjob-cluster-queue"}[30m]))

# Number of running workloads
kueue_admitted_active_workloads{cluster_queue="cjob-cluster-queue"}

# Number of workloads awaiting resource allocation (registered with Kueue, waiting for admission)
sum(kueue_pending_workloads{cluster_queue="cjob-cluster-queue"})

# CPU usage (cores)
kueue_cluster_queue_resource_usage{cluster_queue="cjob-cluster-queue", flavor="cpu", resource="cpu"}

# Memory usage (converted to GiB)
kueue_cluster_queue_resource_usage{cluster_queue="cjob-cluster-queue", flavor="cpu", resource="memory"} / 1024 / 1024 / 1024

# Memory quota limit (converted to GiB)
kueue_cluster_queue_nominal_quota{cluster_queue="cjob-cluster-queue", flavor="cpu", resource="memory"} / 1024 / 1024 / 1024
```

### 4.2 SQL

```sql
-- Recent job wait times (last 6 hours)
SELECT
  namespace AS "User",
  job_id AS "Job ID",
  flavor AS "Flavor",
  cpu || ' CPU / ' || memory || ' / GPU ' || gpu AS "Resources",
  EXTRACT(EPOCH FROM (started_at - created_at))::int AS "Wait time (sec)",
  started_at AS "Start time"
FROM jobs
WHERE status IN ('RUNNING', 'SUCCEEDED', 'FAILED')
  AND started_at IS NOT NULL
  AND created_at >= NOW() - INTERVAL '6 hours'
ORDER BY started_at DESC
LIMIT 15;

-- Job status breakdown (last 24 hours, DISPATCHING excluded)
SELECT
  CASE status
    WHEN 'QUEUED' THEN 'Submitted'
    WHEN 'DISPATCHED' THEN 'Allocation Wait'
    WHEN 'RUNNING' THEN 'Running'
    WHEN 'HELD' THEN 'Held'
    WHEN 'SUCCEEDED' THEN 'Succeeded'
    WHEN 'FAILED' THEN 'Failed'
    WHEN 'CANCELLED' THEN 'Cancelled'
    WHEN 'DELETING' THEN 'Deleting'
    ELSE status
  END AS "Status",
  COUNT(*) AS "Count"
FROM jobs
WHERE created_at >= NOW() - INTERVAL '24 hours'
  AND status != 'DISPATCHING'
GROUP BY status
ORDER BY
  CASE status
    WHEN 'RUNNING' THEN 1
    WHEN 'QUEUED' THEN 2
    WHEN 'HELD' THEN 3
    WHEN 'DISPATCHED' THEN 4
    WHEN 'SUCCEEDED' THEN 5
    WHEN 'FAILED' THEN 6
    WHEN 'CANCELLED' THEN 7
    WHEN 'DELETING' THEN 8
  END;

-- Number of running jobs
SELECT COUNT(*) AS "Running" FROM jobs WHERE status = 'RUNNING';

-- Number of active users
SELECT COUNT(DISTINCT namespace) AS "User count" FROM jobs WHERE status = 'RUNNING';

-- Queue usage by flavor (no query changes needed when adding flavors)
SELECT
  flavor AS "Flavor",
  COUNT(*) FILTER (WHERE status = 'RUNNING') AS "Running",
  COUNT(*) FILTER (WHERE status = 'DISPATCHED') AS "Awaiting Resource Allocation",
  COUNT(*) FILTER (WHERE status = 'QUEUED') AS "Submitted",
  COUNT(*) FILTER (WHERE status = 'HELD') AS "Held"
FROM jobs
WHERE status IN ('RUNNING', 'DISPATCHED', 'QUEUED', 'HELD')
GROUP BY flavor
ORDER BY flavor;

-- Cluster node count
SELECT COUNT(*) AS "Node count" FROM node_resources;
```

## 5. Dashboard JSON

Place the Grafana import JSON file at `k8s/base/grafana/dashboard-user.json`. Deploy it by uploading the JSON file from `Dashboards > Import` in the Grafana UI.

When importing, the data source UIDs must be configured for your environment. Data source references in the JSON use the following variable names:

| Variable name | Data source |
|---|---|
| `${DS_PROMETHEUS}` | Prometheus |
| `${DS_CJOB_DB}` | CJob PostgreSQL |

## 6. Operational Notes

- Kueue metrics reset immediately after a controller restart, causing temporary data gaps. The impact is minimal if the dashboard time range is set appropriately.
- PostgreSQL queries make use of indexes (`idx_jobs_namespace_status`). Pay attention to query performance if a large number of jobs have accumulated (hundreds of thousands or more).
- The `node_resources` table is synced by the Watcher at 300-second intervals, so node additions/removals may take up to 5 minutes to be reflected.
